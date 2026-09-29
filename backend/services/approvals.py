# backend/services/approvals.py
# 高危操作审批：校验 → 逐条落库（谁、何时、批准/修改/拒绝、最终参数）→ 工单回到处理中 → 生成 resume 值。

from dataclasses import dataclass
from typing import Literal, Optional

from backend.agents.ticket.states import TicketStatus
from backend.services import ticket_repo

DecisionType = Literal["approve", "edit", "reject"]
_STATUS = {"approve": "approved", "edit": "edited", "reject": "rejected"}


class ApprovalError(ValueError):
    pass


@dataclass
class Decision:
    approval_id: str
    type: DecisionType
    args: Optional[dict] = None
    comment: Optional[str] = None


async def decide(ticket_id: str, tenant_id: str, interrupt_id: str, decisions: list[Decision],
                 reviewer_id: str, reviewer_name: str) -> dict:
    """返回给 HumanInTheLoopMiddleware 的 resume 值 {"decisions": [...]}，顺序与中断里的工具调用一致。"""
    pending = [a for a in await ticket_repo.list_approvals(tenant_id, "pending", ticket_id)
               if a["interrupt_id"] == interrupt_id]
    by_id = {d.approval_id: d for d in decisions}
    if not pending or set(by_id) != {a["id"] for a in pending}:
        raise ApprovalError("需要对本次全部待审批操作逐条给出决定")
    for d in decisions:
        if d.type == "edit" and not d.args:
            raise ApprovalError("修改审批必须提供新参数")

    resume = []
    for a in sorted(pending, key=lambda x: x["seq"]):
        d = by_id[a["id"]]
        if d.type == "approve":
            resume.append({"type": "approve"})
        elif d.type == "edit":
            resume.append({"type": "edit", "edited_action": {"name": a["tool_name"], "args": d.args}})
        else:
            resume.append({"type": "reject",
                           "message": f"运维拒绝执行 {a['tool_name']}：{d.comment or '未说明原因'}。"
                                      "不要尝试其他方式绕过，请在结论中说明需要人工处理。"})
        final_args = d.args if d.type == "edit" else (a["tool_args"] if d.type == "approve" else None)
        await ticket_repo.decide_approval(a["id"], tenant_id, _STATUS[d.type], reviewer_id, reviewer_name,
                                          final_args, d.comment)

    ticket = await ticket_repo.require_ticket(ticket_id, tenant_id)
    if ticket["status"] == TicketStatus.PENDING_APPROVAL.value:
        summary = "，".join(f"{a['tool_name']}={_STATUS[by_id[a['id']].type]}" for a in pending)
        await ticket_repo.transition(ticket_id, tenant_id, TicketStatus.IN_PROGRESS, reviewer_name or "ops",
                                     f"审批完成：{summary}")
    return {"decisions": resume}
