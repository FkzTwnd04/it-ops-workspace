"""测试桩：可编排的"Agent"和工具调用模型。"""
from typing import Any, Callable, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.config import get_config
from langgraph.types import interrupt

from backend.agents.ticket import tools
from backend.services import ticket_repo


class StubAgent:
    """替代 Deep Agent 的桩：按脚本调用真实的业务工具，高危操作用与 HumanInTheLoopMiddleware 相同格式的 interrupt。

    运行在外层流程图的 run_agent 节点里，审批恢复后节点重跑，interrupt() 返回决定。
    """

    def __init__(self, *, resolved=True, confidence=0.9, retrieval_confidence=0.95,
                 high_risk: Optional[dict] = None, error: Optional[Exception] = None,
                 hook: Optional[Callable] = None, submit: bool = True):
        self.submit = submit
        self.resolved = resolved
        self.confidence = confidence
        self.retrieval_confidence = retrieval_confidence
        self.high_risk = high_risk
        self.error = error
        self.hook = hook
        self.calls = 0

    async def ainvoke(self, _input: Any, *args, **kwargs) -> dict:
        self.calls += 1
        if self.error:
            raise self.error
        cfg = get_config()["configurable"]
        ticket_id, tenant_id = cfg["ticket_id"], cfg["tenant_id"]
        await tools.submit_triage.ainvoke({"category": "network", "priority": "P3",
                                           "summary": "VPN 证书问题", "reason": "测试"})
        if self.retrieval_confidence is not None:
            await ticket_repo.record_retrieval(ticket_id, tenant_id, "测试查询", "runbook", {
                "strategy": "PRECISE", "docs": [{"content": "手册内容", "score": self.retrieval_confidence}],
                "confidence": self.retrieval_confidence,
            })
        if self.hook:
            await self.hook(ticket_id, tenant_id)

        resolved = self.resolved
        if self.high_risk:
            decision = interrupt({
                "action_requests": [{"name": self.high_risk["name"], "args": self.high_risk["args"],
                                     "description": "高危操作"}],
                "review_configs": [{"action_name": self.high_risk["name"],
                                    "allowed_decisions": ["approve", "edit", "reject"]}],
            })["decisions"][0]
            if decision["type"] == "reject":
                resolved = False
            else:
                args = decision["edited_action"]["args"] if decision["type"] == "edit" else self.high_risk["args"]
                await getattr(tools, self.high_risk["name"]).ainvoke(args)

        if not self.submit:
            return {"messages": [AIMessage(content="已排查完毕")]}
        await tools.submit_resolution.ainvoke({
            "resolved": resolved, "root_cause": "证书过期", "summary": "已处理",
            "steps_taken": ["查询日志"], "user_actions": [], "confidence": self.confidence,
        })
        return {"messages": [AIMessage(content="处理完成")]}


class ScriptedToolModel(BaseChatModel):
    """按顺序返回预设 AIMessage 的聊天模型，支持 bind_tools，用于驱动真实的 Deep Agent。"""

    script: list[AIMessage]
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        msg = self.script[min(self.cursor, len(self.script) - 1)]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=msg)])


def tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}])
