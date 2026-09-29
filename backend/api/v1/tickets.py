# backend/api/v1/tickets.py
# 工单接口。提单人（requester）只能看和操作自己的工单；运维（ops / admin）看本部门全部工单并负责审批。
# 所有查询都带 tenant_id，跨部门的工单 ID 一律按 404 处理。

import json
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.agents.ticket.states import CATEGORIES, STATUS_LABELS
from backend.dependencies import get_current_user, require_roles
from backend.services import approvals, ticket_repo, ticket_runner
from backend.services.locks import LockBusyError

router = APIRouter()

OPS_ROLES = ("ops", "admin")


class CreateTicketRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=200)
    description: str = Field(..., min_length=2, max_length=4000)
    host: Optional[str] = Field(None, max_length=128, description="受影响的主机或系统，可不填")
    auto_run: bool = True


class DecisionItem(BaseModel):
    approval_id: str
    type: Literal["approve", "edit", "reject"]
    args: Optional[dict] = Field(None, description="type=edit 时必填：修改后的工具参数")
    comment: Optional[str] = None


class DecideRequest(BaseModel):
    decisions: list[DecisionItem]


class ConfirmRequest(BaseModel):
    resolved: bool
    feedback: Optional[str] = Field(None, max_length=1000)


class HumanResolveRequest(BaseModel):
    action: Literal["resolve", "retry"] = "resolve"
    summary: Optional[str] = Field(None, max_length=2000)
    comment: Optional[str] = Field(None, max_length=1000)


def _is_ops(user: dict) -> bool:
    return user["role"] in OPS_ROLES


async def _load_ticket(ticket_id: str, user: dict) -> dict:
    ticket = await ticket_repo.get_ticket(ticket_id, user["tenant_id"])
    if ticket is None or (not _is_ops(user) and ticket["requester_id"] != user["user_id"]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="工单不存在")
    return ticket


async def _launch(coro) -> None:
    try:
        await coro
    except LockBusyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该工单正在处理中，请稍候")


# ── 工单 ────────────────────────────────────────────────────

@router.get("/meta")
async def meta():
    return {"statuses": STATUS_LABELS, "categories": CATEGORIES}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ticket(req: CreateTicketRequest, user: dict = Depends(get_current_user)):
    ticket = await ticket_repo.create_ticket(
        tenant_id=user["tenant_id"], title=req.title, description=req.description,
        requester_id=user["user_id"], requester_name=user["name"], host=req.host,
    )
    if req.auto_run:
        await _launch(ticket_runner.start(ticket["id"], user["tenant_id"]))
    return ticket


@router.get("")
async def list_tickets(
    status_filter: Optional[str] = Query(None, alias="status"),
    mine: bool = False,
    limit: int = Query(50, le=200),
    offset: int = 0,
    user: dict = Depends(get_current_user),
):
    requester_id = user["user_id"] if (mine or not _is_ops(user)) else None
    return await ticket_repo.list_tickets(user["tenant_id"], status_filter, requester_id, limit, offset)


@router.get("/{ticket_id}")
async def get_ticket(ticket_id: str, user: dict = Depends(get_current_user)):
    ticket = await _load_ticket(ticket_id, user)
    tenant = user["tenant_id"]
    pending = None
    if await ticket_runner.has_started(ticket_id, tenant):
        intr = await ticket_runner.pending_interrupt(ticket_id, tenant)
        pending = intr["kind"] if intr else None
    return {
        "ticket": ticket,
        "events": await ticket_repo.list_events(ticket_id, tenant),
        "approvals": await ticket_repo.list_approvals(tenant, ticket_id=ticket_id),
        "ops_actions": await ticket_repo.list_ops_actions(ticket_id, tenant),
        "retrievals": await ticket_repo.list_retrievals(ticket_id, tenant),
        "pending": pending,
        "running": ticket_runner.is_running(ticket_id),
    }


@router.post("/{ticket_id}/run", status_code=status.HTTP_202_ACCEPTED)
async def run_ticket(ticket_id: str, user: dict = Depends(get_current_user)):
    """首次交给 Agent 处理。已经处理过的工单通过确认 / 审批 / 人工处理接口推进。"""
    await _load_ticket(ticket_id, user)
    if await ticket_runner.has_started(ticket_id, user["tenant_id"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="工单已在处理流程中")
    await _launch(ticket_runner.start(ticket_id, user["tenant_id"]))
    return {"accepted": True}


@router.get("/{ticket_id}/stream")
async def stream_ticket(ticket_id: str, user: dict = Depends(get_current_user)):
    """SSE：推送本工单当前这次运行的进度事件，运行结束发送 done。"""
    await _load_ticket(ticket_id, user)

    async def gen():
        async for ev in ticket_runner.subscribe(ticket_id):
            yield f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/{ticket_id}/confirm", status_code=status.HTTP_202_ACCEPTED)
async def confirm_ticket(ticket_id: str, req: ConfirmRequest, user: dict = Depends(get_current_user)):
    """提单人确认自动方案是否解决了问题。"""
    ticket = await _load_ticket(ticket_id, user)
    if ticket["requester_id"] != user["user_id"] and not _is_ops(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="只有提单人可以确认")
    intr = await ticket_runner.pending_interrupt(ticket_id, user["tenant_id"])
    if not intr or intr["kind"] != "user_confirm":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="工单当前不在待确认状态")
    await _launch(ticket_runner.resume(ticket_id, user["tenant_id"], intr["id"], {
        "resolved": req.resolved, "feedback": req.feedback or "", "actor": user["name"] or "requester",
    }))
    return {"accepted": True}


@router.post("/{ticket_id}/resolve", status_code=status.HTTP_202_ACCEPTED)
async def human_resolve(ticket_id: str, req: HumanResolveRequest, user: dict = Depends(require_roles(*OPS_ROLES))):
    """运维处理已升级的工单：直接给出处理结果结案，或补充说明后退回 Agent 重新处理。"""
    await _load_ticket(ticket_id, user)
    intr = await ticket_runner.pending_interrupt(ticket_id, user["tenant_id"])
    if not intr or intr["kind"] != "human_resolve":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="工单当前不在待人工处理状态")
    if req.action == "resolve" and not req.summary:
        raise HTTPException(status_code=422, detail="请填写处理结果")
    await _launch(ticket_runner.resume(ticket_id, user["tenant_id"], intr["id"], {
        "action": req.action, "summary": req.summary, "comment": req.comment, "actor": user["name"] or "ops",
    }))
    return {"accepted": True}


# ── 审批 ────────────────────────────────────────────────────

approvals_router = APIRouter()


@approvals_router.get("")
async def list_approvals(
    status_filter: Optional[str] = Query("pending", alias="status"),
    user: dict = Depends(require_roles(*OPS_ROLES)),
):
    return await ticket_repo.list_approvals(user["tenant_id"], status_filter or None)


@router.post("/{ticket_id}/approvals", status_code=status.HTTP_202_ACCEPTED)
async def decide_approvals(ticket_id: str, req: DecideRequest, user: dict = Depends(require_roles(*OPS_ROLES))):
    """对一次中断里的全部高危操作逐条给出 批准 / 修改参数后批准 / 拒绝，然后恢复 Agent。"""
    tenant = user["tenant_id"]
    await _load_ticket(ticket_id, user)
    if ticket_runner.is_running(ticket_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该工单正在处理中，请稍候")
    intr = await ticket_runner.pending_interrupt(ticket_id, tenant)
    if not intr or intr["kind"] != "approval":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该工单没有待审批的操作")

    try:
        resume_value = await approvals.decide(
            ticket_id, tenant, intr["id"],
            [approvals.Decision(d.approval_id, d.type, d.args, d.comment) for d in req.decisions],
            user["user_id"], user["name"],
        )
    except approvals.ApprovalError as e:
        raise HTTPException(status_code=422, detail=str(e))
    await _launch(ticket_runner.resume(ticket_id, tenant, intr["id"], resume_value))
    return {"accepted": True}
