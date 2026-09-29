# backend/agents/ticket/workflow.py
# 外层工单流程图：状态机驱动 Deep Agent。
#
#   start_run → run_agent → finalize ─┬─ 自动解决 → await_confirm ─┬─ 确认 → RESOLVED → CLOSED
#                                     │                            ├─ 未解决（首次）→ 重新处理
#                                     │                            └─ 未解决（再次）→ ESCALATED
#                                     └─ 转人工 → await_human ─────┬─ 人工处理 → RESOLVED → CLOSED
#                                                                  └─ 重新交给 Agent
#
# 模型只负责诊断和提交结论；"能不能自动结案"由 finalize 按代码规则判定，
# 状态变更一律走 ticket_repo.transition 的白名单校验。
# 三层降级：模型调用重试（ModelRetryMiddleware）→ 仅检索参考方案并转人工 → 系统兜底转人工。

from typing import Any, Optional, TypedDict

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.errors import GraphBubbleUp
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from backend.agents.ticket.agent import build_ticket_agent
from backend.agents.ticket.retrieval import search_knowledge
from backend.agents.ticket.states import TicketStatus, can_transition
from backend.agents.ticket.tools import submit_resolution
from backend.config import get_settings
from backend.core.llm_factory import get_llm
from backend.core.logger import get_logger
from backend.core.retry import system_fallback
from backend.services import ticket_repo

logger = get_logger(__name__)
settings = get_settings()

MAX_REOPEN = 1


class TicketFlowState(TypedDict, total=False):
    ticket_id: str
    tenant_id: str
    attempt: int
    feedback: str
    run_id: int
    fallback_level: int
    error: Optional[str]
    agent_reply: str
    outcome: str


_agent = None


def _get_agent():
    global _agent
    if _agent is None:
        _agent = build_ticket_agent()
    return _agent


def set_agent_for_tests(agent) -> None:
    global _agent
    _agent = agent


def _task_message(ticket: dict, username: Optional[str], state: TicketFlowState) -> str:
    text = (f"请处理工单 {ticket['ticket_no']}\n"
            f"标题：{ticket['title']}\n描述：{ticket['description']}\n"
            f"受影响主机：{ticket['host'] or '未填写'}\n"
            f"提单人：{ticket['requester_name'] or '未知'}（域账号 {username or '未知'}）")
    if state.get("attempt"):
        text += (f"\n\n这是第 {state['attempt'] + 1} 次处理。上次给出的方案用户反馈未解决："
                 f"{state.get('feedback') or '未说明原因'}。请重新诊断，不要重复上次无效的操作。")
    return text


def _last_ai_text(result: dict) -> str:
    for msg in reversed(result.get("messages", [])):
        if isinstance(msg, AIMessage) and msg.content and not msg.tool_calls:
            return msg.content if isinstance(msg.content, str) else str(msg.content)
    return ""


# ── 节点 ────────────────────────────────────────────────────

async def start_run(state: TicketFlowState) -> dict:
    run_id = await ticket_repo.start_run(state["ticket_id"], state["tenant_id"])
    # 重新处理时清掉上一轮结论，避免本轮没提交结论时 finalize 误用旧结论
    await ticket_repo.update_fields(state["ticket_id"], state["tenant_id"], resolution=None)
    return {"run_id": run_id, "fallback_level": 0, "error": None, "agent_reply": ""}


async def run_agent(state: TicketFlowState) -> dict:
    # 审批恢复时本节点会从头重跑，Deep Agent 子图从 checkpoint 续跑，这里不能有非幂等副作用
    ticket = await ticket_repo.require_ticket(state["ticket_id"], state["tenant_id"])
    username = await ticket_repo.get_username(ticket["requester_id"], state["tenant_id"])
    try:
        result = await _get_agent().ainvoke(
            {"messages": [HumanMessage(content=_task_message(ticket, username, state))]}
        )
        await _ensure_resolution(ticket, result)
        return {"fallback_level": 0, "agent_reply": _last_ai_text(result)}
    except GraphBubbleUp:
        raise
    except Exception as e:
        logger.warning("ticket_agent.failed", ticket_id=state["ticket_id"], error=str(e))
        return await _degrade(ticket, e)


_FINISH_PROMPT = ("你是 IT 工单处理 Agent。上面是你处理这张工单的完整过程，但你结束前没有提交结论。"
                  "不要再做诊断或运维操作，只根据已有的检索、日志和操作结果调用一次 submit_resolution。")


async def _ensure_resolution(ticket: dict, result: dict) -> None:
    """Agent 结束时没调用 submit_resolution：基于它的处理记录强制补交一次结论（不能再次调用 Agent 子图）。"""
    current = await ticket_repo.require_ticket(str(ticket["id"]), ticket["tenant_id"])
    if current["resolution"] or not result.get("messages"):
        return
    logger.info("ticket_agent.missing_resolution", ticket_id=str(ticket["id"]))
    model = get_llm("ticket_subagent").bind_tools([submit_resolution], tool_choice="submit_resolution")
    reply = await model.ainvoke([*result["messages"], HumanMessage(content=_FINISH_PROMPT)])
    for call in getattr(reply, "tool_calls", None) or []:
        if call["name"] == "submit_resolution":
            await submit_resolution.ainvoke(call["args"])
            return


async def _degrade(ticket: dict, error: Exception) -> dict:
    ticket_id, tenant_id = str(ticket["id"]), ticket["tenant_id"]
    result = await search_knowledge(f"{ticket['title']}\n{ticket['description']}", tenant_id, doc_type="runbook")
    if result["docs"] and not result["fallback_used"]:
        await ticket_repo.update_fields(ticket_id, tenant_id, fallback_used=True, resolution={
            "resolved": False,
            "confidence": 0.0,
            "summary": "智能诊断暂不可用，已附上知识库中的参考方案，工单转运维人员处理。",
            "references": [{"source": d["source_name"], "score": d["score"], "content": d["content"]}
                           for d in result["docs"]],
        })
        return {"fallback_level": 2, "error": str(error)}

    fallback = system_fallback("ticket_agent", error)
    await ticket_repo.update_fields(ticket_id, tenant_id, fallback_used=True, resolution={
        "resolved": False, "confidence": 0.0, "summary": fallback["content"],
    })
    return {"fallback_level": 3, "error": str(error)}


def _escalation_reason(level: int, resolution: dict, retrieval_conf: float) -> Optional[str]:
    """返回转人工原因；返回 None 表示满足自动解决条件。"""
    if level == 3:
        return "系统降级：智能处理服务不可用"
    if level == 2:
        return "智能诊断失败，已附知识库参考方案"
    if not resolution:
        return "Agent 未提交处理结论"
    if not resolution.get("resolved"):
        return f"Agent 判断无法自动解决：{(resolution.get('summary') or '')[:80]}"
    conf = float(resolution.get("confidence") or 0)
    if conf < settings.resolution_confidence_threshold:
        return f"结论置信度 {conf:.2f} 低于阈值 {settings.resolution_confidence_threshold}"
    if retrieval_conf < settings.retrieval_confidence_threshold:
        return f"知识库检索置信度 {retrieval_conf:.2f} 低于阈值 {settings.retrieval_confidence_threshold}，需人工复核"
    return None


async def finalize(state: TicketFlowState) -> dict:
    ticket_id, tenant_id = state["ticket_id"], state["tenant_id"]
    ticket = await ticket_repo.require_ticket(ticket_id, tenant_id)
    status = TicketStatus(ticket["status"])
    level = state.get("fallback_level", 0)
    retrieval_conf = float(ticket["retrieval_confidence"] or 0)
    reason = _escalation_reason(level, ticket["resolution"] or {}, retrieval_conf)

    if status == TicketStatus.ESCALATED:
        outcome = "escalated"          # 处理过程中已被 SLA 巡检升级，保持升级状态
    elif reason is None and status == TicketStatus.IN_PROGRESS:
        await ticket_repo.transition(
            ticket_id, tenant_id, TicketStatus.PENDING_CONFIRM, "ticket_agent",
            f"自动解决，待提单人确认（检索置信度 {retrieval_conf:.2f}）", auto_resolved=True,
        )
        outcome = "pending_confirm"
    elif can_transition(status, TicketStatus.ESCALATED):
        reason = reason or f"工单状态 {status.value} 不满足自动解决条件"
        await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.ESCALATED, "ticket_agent",
                                     reason, escalation_reason=reason)
        outcome = "escalated"
    else:
        outcome = status.value.lower()

    await ticket_repo.finish_run(state["run_id"], outcome, level, retrieval_conf, state.get("error"))
    return {"outcome": outcome}


async def await_confirm(state: TicketFlowState) -> dict:
    decision: dict[str, Any] = interrupt({"kind": "user_confirm", "ticket_id": state["ticket_id"]})
    ticket_id, tenant_id = state["ticket_id"], state["tenant_id"]
    actor = decision.get("actor") or "requester"

    if decision.get("resolved"):
        await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.RESOLVED, actor, "提单人确认已解决")
        await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.CLOSED, "system", "确认解决后自动关闭")
        return {"outcome": "closed"}

    feedback = decision.get("feedback") or ""
    attempt = state.get("attempt", 0)
    if attempt < MAX_REOPEN:
        ticket = await ticket_repo.require_ticket(ticket_id, tenant_id)
        await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.IN_PROGRESS, actor,
                                     f"提单人反馈未解决：{feedback}", auto_resolved=False,
                                     reopen_count=ticket["reopen_count"] + 1)
        return {"outcome": "retry", "attempt": attempt + 1, "feedback": feedback}

    reason = f"自动方案 {attempt + 1} 次未解决，转人工：{feedback}"
    await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.ESCALATED, actor, reason,
                                 auto_resolved=False, escalation_reason=reason)
    return {"outcome": "escalated"}


async def await_human(state: TicketFlowState) -> dict:
    decision: dict[str, Any] = interrupt({"kind": "human_resolve", "ticket_id": state["ticket_id"]})
    ticket_id, tenant_id = state["ticket_id"], state["tenant_id"]
    actor = decision.get("actor") or "ops"

    if decision.get("action") == "retry":
        await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.IN_PROGRESS, actor,
                                     decision.get("comment") or "运维退回，重新交给 Agent 处理")
        return {"outcome": "retry", "feedback": decision.get("comment") or ""}

    ticket = await ticket_repo.require_ticket(ticket_id, tenant_id)
    resolution = {**(ticket["resolution"] or {}),
                  "resolved": True, "human_summary": decision.get("summary") or "", "resolved_by": actor}
    await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.RESOLVED, actor, "人工处理完成",
                                 resolution=resolution, auto_resolved=False)
    await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.CLOSED, "system", "人工处理完成后关闭")
    return {"outcome": "closed"}


# ── 图 ─────────────────────────────────────────────────────

def build_workflow(checkpointer):
    g = StateGraph(TicketFlowState)
    g.add_node("start_run", start_run)
    g.add_node("run_agent", run_agent)
    g.add_node("finalize", finalize)
    g.add_node("await_confirm", await_confirm)
    g.add_node("await_human", await_human)

    g.add_edge(START, "start_run")
    g.add_edge("start_run", "run_agent")
    g.add_edge("run_agent", "finalize")
    g.add_conditional_edges(
        "finalize",
        lambda s: s["outcome"] if s["outcome"] in ("pending_confirm", "escalated") else "end",
        {"pending_confirm": "await_confirm", "escalated": "await_human", "end": END},
    )
    g.add_conditional_edges("await_confirm", lambda s: s["outcome"],
                            {"closed": END, "retry": "start_run", "escalated": "await_human"})
    g.add_conditional_edges("await_human", lambda s: s["outcome"],
                            {"closed": END, "retry": "start_run"})
    return g.compile(checkpointer=checkpointer, name="ticket-workflow")


_workflow = None


def get_workflow():
    global _workflow
    if _workflow is None:
        from backend.core.checkpointer import get_checkpointer
        _workflow = build_workflow(get_checkpointer())
    return _workflow


def reset_workflow_for_tests() -> None:
    global _workflow
    _workflow = None
