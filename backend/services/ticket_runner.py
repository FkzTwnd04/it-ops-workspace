# backend/services/ticket_runner.py
# 工单流程的运行调度：
#   - 每次启动 / 恢复都在后台任务里跑，HTTP 请求立即返回；同一工单用分布式锁保证只有一次运行
#   - 运行过程被翻译成进度事件（待办、子 Agent 委派、工具调用、状态变化），推给订阅该工单的 SSE 连接
#   - 流程停在 interrupt 时按中断类型落库：高危操作 → 审批单 + PENDING_APPROVAL；
#     待用户确认 / 待人工处理只推事件，等对应接口 resume
# 事件总线在进程内，单实例部署；多实例时换成 Redis Pub/Sub。

import asyncio
import json
import time
from contextlib import AsyncExitStack
from typing import Any, AsyncIterator, Optional

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.types import Command

from backend.agents.ticket.states import TicketStatus
from backend.agents.ticket.tools import ticket_config
from backend.agents.ticket.workflow import get_workflow
from backend.core.logger import get_logger
from backend.services import ticket_repo
from backend.services.locks import distributed_lock

logger = get_logger(__name__)

RUN_LOCK_TTL = 900
_MAX_TEXT = 600

_history: dict[str, list[dict]] = {}
_subscribers: dict[str, set[asyncio.Queue]] = {}
_running: set[str] = set()


# ── 事件总线 ────────────────────────────────────────────────

def _publish(ticket_id: str, event: dict) -> None:
    event = {"ts": round(time.time(), 3), **event}
    _history.setdefault(ticket_id, []).append(event)
    for q in list(_subscribers.get(ticket_id, ())):
        q.put_nowait(event)


async def subscribe(ticket_id: str) -> AsyncIterator[dict]:
    """先回放本次运行已产生的事件，再持续推送；运行结束（done）后退出。"""
    q: asyncio.Queue = asyncio.Queue()
    for ev in _history.get(ticket_id, []):
        q.put_nowait(ev)
    _subscribers.setdefault(ticket_id, set()).add(q)
    try:
        if ticket_id not in _running and not _history.get(ticket_id):
            yield {"type": "done", "idle": True}
            return
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=15)
            except asyncio.TimeoutError:
                yield {"type": "ping"}
                continue
            yield ev
            if ev["type"] == "done":
                return
    finally:
        _subscribers.get(ticket_id, set()).discard(q)


def is_running(ticket_id: str) -> bool:
    return ticket_id in _running


# ── 中断查询 ────────────────────────────────────────────────

async def pending_interrupt(ticket_id: str, tenant_id: str) -> Optional[dict]:
    """返回当前挂起的中断：{"id", "kind", "value"}；kind ∈ approval / user_confirm / human_resolve。"""
    snapshot = await get_workflow().aget_state(ticket_config(ticket_id, tenant_id))
    for intr in snapshot.interrupts:
        value = intr.value if isinstance(intr.value, dict) else {}
        kind = "approval" if "action_requests" in value else value.get("kind")
        return {"id": intr.id, "kind": kind, "value": value}
    return None


async def has_started(ticket_id: str, tenant_id: str) -> bool:
    snapshot = await get_workflow().aget_state(ticket_config(ticket_id, tenant_id))
    return bool(snapshot.values)


# ── 启动 / 恢复 ─────────────────────────────────────────────

async def launch(ticket_id: str, tenant_id: str, graph_input: Any) -> None:
    """拿到工单锁后放到后台运行；锁被占用时抛 LockBusyError（接口返回 409）。"""
    stack = AsyncExitStack()
    await stack.enter_async_context(distributed_lock(f"ticket:{ticket_id}", ttl_seconds=RUN_LOCK_TTL))
    _running.add(ticket_id)
    _history[ticket_id] = []
    asyncio.create_task(_run(stack, ticket_id, tenant_id, graph_input))


async def start(ticket_id: str, tenant_id: str) -> None:
    await launch(ticket_id, tenant_id, {"ticket_id": ticket_id, "tenant_id": tenant_id, "attempt": 0})


async def resume(ticket_id: str, tenant_id: str, interrupt_id: str, value: Any) -> None:
    await launch(ticket_id, tenant_id, Command(resume={interrupt_id: value}))


async def run_to_pause(ticket_id: str, tenant_id: str, graph_input: Any) -> Optional[dict]:
    """同步跑到下一个中断或结束（评测脚本和测试用），返回挂起的中断。"""
    async with distributed_lock(f"ticket:{ticket_id}", ttl_seconds=RUN_LOCK_TTL):
        _running.add(ticket_id)
        _history[ticket_id] = []
        try:
            return await _drive(ticket_id, tenant_id, graph_input)
        finally:
            _running.discard(ticket_id)


async def _run(stack: AsyncExitStack, ticket_id: str, tenant_id: str, graph_input: Any) -> None:
    try:
        await _drive(ticket_id, tenant_id, graph_input)
    except Exception as e:
        logger.error("ticket_runner.failed", ticket_id=ticket_id, error=str(e))
        _publish(ticket_id, {"type": "error", "message": "处理过程出现异常，工单状态已保存，可稍后重试"})
        _publish(ticket_id, {"type": "done"})
    finally:
        _running.discard(ticket_id)
        await stack.aclose()


async def _drive(ticket_id: str, tenant_id: str, graph_input: Any) -> Optional[dict]:
    config = ticket_config(ticket_id, tenant_id)
    translator = _Translator(ticket_id)
    last_status = (await ticket_repo.require_ticket(ticket_id, tenant_id))["status"]
    _publish(ticket_id, {"type": "status", "status": last_status})

    async for ns, mode, chunk in get_workflow().astream(
        graph_input, config, stream_mode=["updates", "messages"], subgraphs=True
    ):
        if mode == "messages":
            translator.learn_agent_name(chunk[1])
            continue
        translator.handle(ns, chunk)
        status = (await ticket_repo.require_ticket(ticket_id, tenant_id))["status"]
        if status != last_status:
            last_status = status
            _publish(ticket_id, {"type": "status", "status": status})

    intr = await pending_interrupt(ticket_id, tenant_id)
    if intr and intr["kind"] == "approval":
        await _on_approval_interrupt(ticket_id, tenant_id, intr)
    elif intr:
        _publish(ticket_id, {"type": "awaiting", "kind": intr["kind"]})

    ticket = await ticket_repo.require_ticket(ticket_id, tenant_id)
    _publish(ticket_id, {"type": "status", "status": ticket["status"]})
    _publish(ticket_id, {"type": "done", "pending": intr["kind"] if intr else None})
    return intr


async def _on_approval_interrupt(ticket_id: str, tenant_id: str, intr: dict) -> None:
    created = await ticket_repo.create_approvals(ticket_id, tenant_id, intr["id"],
                                                 intr["value"]["action_requests"])
    ticket = await ticket_repo.require_ticket(ticket_id, tenant_id)
    if created and ticket["status"] == TicketStatus.IN_PROGRESS.value:
        tools = "、".join(a["tool_name"] for a in created)
        await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.PENDING_APPROVAL,
                                     "ticket_agent", f"高危操作待审批：{tools}")
    _publish(ticket_id, {"type": "awaiting", "kind": "approval",
                         "actions": [{"name": r["name"], "args": r.get("args", {})}
                                     for r in intr["value"]["action_requests"]]})


# ── 流事件翻译 ──────────────────────────────────────────────

def _clip(text: Any) -> str:
    s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False, default=str)
    return s if len(s) <= _MAX_TEXT else s[:_MAX_TEXT] + "…"


class _Translator:
    """把 astream(subgraphs=True) 的 updates 翻译成前端进度事件。

    namespace 深度：0 = 外层工单流程，1 = 主 Agent，≥2 = 子 Agent。
    子 Agent 的名字从 messages 流的 lc_agent_name 元数据学到。
    """

    def __init__(self, ticket_id: str):
        self.ticket_id = ticket_id
        self.agent_by_ns: dict[str, str] = {}

    def learn_agent_name(self, metadata: dict) -> None:
        # messages 流的 checkpoint_ns 是节点级（…|model:<id>），去掉最后一段即 updates 流的 namespace
        name = metadata.get("lc_agent_name")
        ns = metadata.get("langgraph_checkpoint_ns") or ""
        if name and ns:
            self.agent_by_ns.setdefault(ns.rsplit("|", 1)[0], name)

    def _agent(self, ns: tuple) -> str:
        if len(ns) <= 1:
            return "ticket-agent"
        return self.agent_by_ns.get("|".join(ns), "subagent")

    def handle(self, ns: tuple, chunk: dict) -> None:
        emit = lambda ev: _publish(self.ticket_id, ev)  # noqa: E731
        for node, update in chunk.items():
            if node == "__interrupt__":
                continue
            if not ns:
                emit({"type": "stage", "stage": node})
                continue
            if not isinstance(update, dict):
                continue
            agent = self._agent(ns)
            if update.get("todos") is not None and len(ns) == 1:
                emit({"type": "todos", "todos": update["todos"]})
            for msg in _as_list(update.get("messages")):
                if isinstance(msg, AIMessage):
                    for call in msg.tool_calls:
                        if call["name"] == "task":
                            emit({"type": "subagent_start", "agent": agent,
                                  "subagent": call["args"].get("subagent_type"),
                                  "description": _clip(call["args"].get("description", ""))})
                        elif call["name"] != "write_todos":
                            emit({"type": "tool_call", "agent": agent, "tool": call["name"],
                                  "args": call["args"]})
                    if msg.content and not msg.tool_calls:
                        emit({"type": "agent_message", "agent": agent, "content": _clip(msg.content)})
                elif isinstance(msg, ToolMessage):
                    if msg.name == "task":
                        emit({"type": "subagent_result", "agent": agent, "content": _clip(msg.content)})
                    elif msg.name != "write_todos":
                        emit({"type": "tool_result", "agent": agent, "tool": msg.name,
                              "content": _clip(msg.content)})


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, BaseMessage):
        return [value]
    return []
