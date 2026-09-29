"""数据层：状态流转落库 + 审计、并发流转行锁、租户隔离、SLA 巡检、统计。"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from backend.agents.ticket.states import InvalidTransitionError, TicketStatus as S
from backend.services import sla, ticket_repo

pytestmark = pytest.mark.db


async def test_create_ticket_records_new_event(new_ticket, tenant):
    t = await new_ticket()
    assert t["status"] == "NEW" and t["ticket_no"].startswith("INC")
    assert t["sla_due_at"] - t["created_at"] == timedelta(hours=8)   # 默认 P3
    events = await ticket_repo.list_events(t["id"], tenant)
    assert [(e["from_status"], e["to_status"]) for e in events] == [(None, "NEW")]


async def test_transition_updates_version_and_audit(new_ticket, tenant):
    t = await new_ticket()
    t2 = await ticket_repo.transition(t["id"], tenant, S.TRIAGED, "tester", "分类完成", category="network")
    assert t2["status"] == "TRIAGED" and t2["version"] == t["version"] + 1 and t2["category"] == "network"
    last = (await ticket_repo.list_events(t["id"], tenant))[-1]
    assert (last["from_status"], last["to_status"], last["actor"], last["reason"]) == \
           ("NEW", "TRIAGED", "tester", "分类完成")


async def test_invalid_transition_rolls_back(new_ticket, tenant):
    t = await new_ticket()
    with pytest.raises(InvalidTransitionError):
        await ticket_repo.transition(t["id"], tenant, S.CLOSED, "tester")
    after = await ticket_repo.require_ticket(t["id"], tenant)
    assert after["status"] == "NEW" and after["version"] == t["version"]
    assert len(await ticket_repo.list_events(t["id"], tenant)) == 1


async def test_resolved_and_closed_timestamps(new_ticket, tenant):
    t = await new_ticket()
    for s in (S.ESCALATED, S.RESOLVED, S.CLOSED):
        t = await ticket_repo.transition(t["id"], tenant, s, "tester")
    assert t["resolved_at"] is not None and t["closed_at"] is not None


async def test_concurrent_transitions_are_serialized(new_ticket, tenant):
    """两个并发请求都想把 NEW 推进到 TRIAGED：行锁保证只有一个成功，另一个看到新状态后被白名单拒绝。"""
    t = await new_ticket()
    results = await asyncio.gather(
        ticket_repo.transition(t["id"], tenant, S.TRIAGED, "a"),
        ticket_repo.transition(t["id"], tenant, S.TRIAGED, "b"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, InvalidTransitionError) for r in results) == 1
    assert len(await ticket_repo.list_events(t["id"], tenant)) == 2


async def test_update_fields_whitelist(new_ticket, tenant):
    t = await new_ticket()
    with pytest.raises(ValueError):
        await ticket_repo.update_fields(t["id"], tenant, status="CLOSED")
    with pytest.raises(ValueError):
        await ticket_repo.update_fields(t["id"], tenant, requester_id="other")


async def test_tenant_isolation_on_read_and_write(new_ticket, tenant, other_tenant):
    t = await new_ticket()
    assert await ticket_repo.get_ticket(t["id"], other_tenant) is None
    with pytest.raises(ticket_repo.TicketNotFoundError):
        await ticket_repo.transition(t["id"], other_tenant, S.TRIAGED, "attacker")
    assert all(x["id"] != t["id"] for x in await ticket_repo.list_tickets(other_tenant))
    assert (await ticket_repo.require_ticket(t["id"], tenant))["status"] == "NEW"


async def test_retrieval_confidence_keeps_max(new_ticket, tenant):
    t = await new_ticket()
    for c in (0.4, 0.9, 0.2):
        await ticket_repo.record_retrieval(t["id"], tenant, "q", "runbook",
                                           {"strategy": "PRECISE", "docs": [], "confidence": c})
    assert (await ticket_repo.require_ticket(t["id"], tenant))["retrieval_confidence"] == pytest.approx(0.9)
    assert len(await ticket_repo.list_retrievals(t["id"], tenant)) == 3


async def test_approvals_idempotent_per_interrupt(new_ticket, tenant):
    t = await new_ticket()
    reqs = [{"name": "restart_service", "args": {"host": "h"}}, {"name": "reset_password", "args": {"username": "u"}}]
    created = await ticket_repo.create_approvals(t["id"], tenant, "intr-1", reqs)
    assert [a["seq"] for a in created] == [0, 1]
    assert await ticket_repo.create_approvals(t["id"], tenant, "intr-1", reqs) == []
    assert len(await ticket_repo.list_approvals(tenant, "pending", t["id"])) == 2


async def test_decide_approval_only_once(new_ticket, tenant, ops):
    t = await new_ticket()
    a = (await ticket_repo.create_approvals(t["id"], tenant, "i", [{"name": "restart_service", "args": {}}]))[0]
    await ticket_repo.decide_approval(a["id"], tenant, "approved", ops["user_id"], "ops", {}, None)
    with pytest.raises(ValueError):
        await ticket_repo.decide_approval(a["id"], tenant, "rejected", ops["user_id"], "ops", None, None)
    row = (await ticket_repo.list_approvals(tenant, ticket_id=t["id"]))[0]
    assert row["status"] == "approved" and row["reviewer_name"] == "ops" and row["decided_at"]


async def test_sla_scan_escalates_only_breached_tracked_tickets(new_ticket, tenant):
    past = datetime.now(timezone.utc) - timedelta(minutes=1)
    breached = await new_ticket()
    await ticket_repo.update_fields(breached["id"], tenant, sla_due_at=past)
    waiting_user = await new_ticket()
    for s in (S.TRIAGED, S.IN_PROGRESS, S.PENDING_CONFIRM):
        await ticket_repo.transition(waiting_user["id"], tenant, s, "t")
    await ticket_repo.update_fields(waiting_user["id"], tenant, sla_due_at=past)
    fresh = await new_ticket()

    escalated = await sla.scan_once()
    assert breached["ticket_no"] in escalated
    b = await ticket_repo.require_ticket(breached["id"], tenant)
    assert b["status"] == "ESCALATED" and "SLA" in b["escalation_reason"]
    assert (await ticket_repo.list_events(breached["id"], tenant))[-1]["actor"] == "sla_monitor"
    assert (await ticket_repo.require_ticket(waiting_user["id"], tenant))["status"] == "PENDING_CONFIRM"
    assert (await ticket_repo.require_ticket(fresh["id"], tenant))["status"] == "NEW"


async def test_run_latency_excludes_approval_wait(new_ticket, tenant):
    from sqlalchemy import text

    from backend.dependencies import AsyncSessionLocal
    t = await new_ticket()
    run_id = await ticket_repo.start_run(t["id"], tenant)
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(text("UPDATE ticket_runs SET started_at = NOW() - INTERVAL '100 seconds' WHERE id = :id"),
                         {"id": run_id})
        await db.execute(text(
            "INSERT INTO ticket_approvals (ticket_id, tenant_id, tool_name, tool_args, status, requested_at, decided_at) "
            "VALUES (:t, :tenant, 'restart_service', '{}', 'approved', NOW() - INTERVAL '90 seconds', "
            "NOW() - INTERVAL '10 seconds')"), {"t": t["id"], "tenant": tenant})
    await ticket_repo.finish_run(run_id, "pending_confirm")
    async with AsyncSessionLocal() as db:
        row = (await db.execute(text("SELECT latency_ms, approvals FROM ticket_runs WHERE id = :id"),
                                {"id": run_id})).fetchone()
    assert 19_000 <= row.latency_ms <= 22_000     # 100s 总时长 - 80s 审批等待
    assert row.approvals == 1


async def test_stats(new_ticket, tenant):
    t = await new_ticket()
    run_id = await ticket_repo.start_run(t["id"], tenant)
    await ticket_repo.finish_run(run_id, "escalated", fallback_level=2)
    s = await ticket_repo.stats(tenant)
    assert s["runs"] >= 1 and s["fallback_rate"] > 0 and s["latency_p95_ms"] is not None
