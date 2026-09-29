"""外层工单状态机端到端：自动解决 / 转人工 / 审批 / 重新处理 / 降级 / SLA / Checkpoint 恢复。

Agent 用桩替换（调用真实业务工具、真实落库），检验的是状态机和编排逻辑本身。
"""
from datetime import datetime, timedelta, timezone

import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command

from backend.agents.ticket import workflow
from backend.services import approvals, sla, ticket_repo, ticket_runner
from tests.stubs import ScriptedToolModel, StubAgent, tool_call

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("memory_workflow")]

RESTART = {"name": "restart_service",
           "args": {"host": "vpn-gw-01", "service": "vpn-gateway", "reason": "地址池耗尽"}}


async def _start(ticket, tenant):
    return await ticket_runner.run_to_pause(ticket["id"], tenant,
                                            {"ticket_id": ticket["id"], "tenant_id": tenant, "attempt": 0})


async def _resume(ticket, tenant, intr, value):
    return await ticket_runner.run_to_pause(ticket["id"], tenant, Command(resume={intr["id"]: value}))


async def _statuses(ticket, tenant):
    return [e["to_status"] for e in await ticket_repo.list_events(ticket["id"], tenant)]


async def _approve_all(ticket, tenant, intr, kind="approve", args=None):
    pending = await ticket_repo.list_approvals(tenant, "pending", ticket["id"])
    return await approvals.decide(ticket["id"], tenant, intr["id"],
                                  [approvals.Decision(a["id"], kind, args) for a in pending], None, "ops")


async def test_auto_resolve_then_user_confirms(new_ticket, tenant):
    workflow.set_agent_for_tests(StubAgent())
    t = await new_ticket()
    intr = await _start(t, tenant)
    assert intr["kind"] == "user_confirm"
    ticket = await ticket_repo.require_ticket(t["id"], tenant)
    assert ticket["status"] == "PENDING_CONFIRM" and ticket["auto_resolved"]

    assert await _resume(t, tenant, intr, {"resolved": True, "actor": "req"}) is None
    assert await _statuses(t, tenant) == ["NEW", "TRIAGED", "IN_PROGRESS", "PENDING_CONFIRM", "RESOLVED", "CLOSED"]


@pytest.mark.parametrize("stub,reason_part", [
    (StubAgent(retrieval_confidence=0.3), "检索置信度"),
    (StubAgent(confidence=0.5), "结论置信度"),
    (StubAgent(resolved=False), "无法自动解决"),
    (StubAgent(retrieval_confidence=None), "检索置信度"),
])
async def test_rules_route_to_human(stub, reason_part, new_ticket, tenant):
    """是否自动结案由代码规则决定，模型自评 resolved=True 也要过置信度门槛。"""
    workflow.set_agent_for_tests(stub)
    t = await new_ticket()
    intr = await _start(t, tenant)
    assert intr["kind"] == "human_resolve"
    ticket = await ticket_repo.require_ticket(t["id"], tenant)
    assert ticket["status"] == "ESCALATED" and reason_part in ticket["escalation_reason"]


async def test_missing_resolution_is_backfilled(new_ticket, tenant, monkeypatch):
    """Agent 结束前没提交结论：强制补交一次，再按规则判定。"""
    finisher = ScriptedToolModel(script=[tool_call("submit_resolution", {
        "resolved": True, "root_cause": "旧密码缓存", "summary": "按手册重新连接",
        "steps_taken": ["检索手册"], "user_actions": ["忘记网络后重连"], "confidence": 0.85}, "f1")])
    monkeypatch.setattr(workflow, "get_llm", lambda *a, **k: finisher)
    workflow.set_agent_for_tests(StubAgent(submit=False))
    t = await new_ticket()
    intr = await _start(t, tenant)
    assert intr["kind"] == "user_confirm" and finisher.cursor == 1
    assert (await ticket_repo.require_ticket(t["id"], tenant))["resolution"]["root_cause"] == "旧密码缓存"


async def test_stale_resolution_cleared_on_reprocess(new_ticket, tenant, monkeypatch):
    """重新处理时本轮没给出结论，不能沿用上一轮的"已解决"结论。"""
    finisher = ScriptedToolModel(script=[AIMessage(content="不提交")])
    monkeypatch.setattr(workflow, "get_llm", lambda *a, **k: finisher)
    stub = StubAgent()
    workflow.set_agent_for_tests(stub)
    t = await new_ticket()
    intr = await _start(t, tenant)
    stub.submit = False
    intr = await _resume(t, tenant, intr, {"resolved": False, "feedback": "没解决"})
    assert intr["kind"] == "human_resolve"
    assert "未提交处理结论" in (await ticket_repo.require_ticket(t["id"], tenant))["escalation_reason"]


async def test_human_resolves_escalated_ticket(new_ticket, tenant):
    workflow.set_agent_for_tests(StubAgent(resolved=False))
    t = await new_ticket()
    intr = await _start(t, tenant)
    assert await _resume(t, tenant, intr, {"action": "resolve", "summary": "现场更换网线", "actor": "王工"}) is None
    ticket = await ticket_repo.require_ticket(t["id"], tenant)
    assert ticket["status"] == "CLOSED" and not ticket["auto_resolved"]
    assert ticket["resolution"]["human_summary"] == "现场更换网线" and ticket["resolution"]["resolved_by"] == "王工"


async def test_human_sends_back_to_agent(new_ticket, tenant):
    stub = StubAgent(resolved=False)
    workflow.set_agent_for_tests(stub)
    t = await new_ticket()
    intr = await _start(t, tenant)
    stub.resolved = True
    intr = await _resume(t, tenant, intr, {"action": "retry", "comment": "补充：用户在外地", "actor": "王工"})
    assert intr["kind"] == "user_confirm" and stub.calls == 2


async def test_high_risk_requires_approval_then_executes(new_ticket, tenant):
    workflow.set_agent_for_tests(StubAgent(high_risk=RESTART))
    t = await new_ticket()
    intr = await _start(t, tenant)
    assert intr["kind"] == "approval"
    assert (await ticket_repo.require_ticket(t["id"], tenant))["status"] == "PENDING_APPROVAL"
    assert await ticket_repo.list_ops_actions(t["id"], tenant) == []          # 审批前不执行

    value = await _approve_all(t, tenant, intr)
    assert (await ticket_repo.require_ticket(t["id"], tenant))["status"] == "IN_PROGRESS"
    intr = await _resume(t, tenant, intr, value)
    assert intr["kind"] == "user_confirm"
    ops = await ticket_repo.list_ops_actions(t["id"], tenant)
    assert [o["tool_name"] for o in ops] == ["restart_service"]
    audit = (await ticket_repo.list_approvals(tenant, ticket_id=t["id"]))[0]
    assert audit["status"] == "approved" and audit["reviewer_name"] == "ops" and audit["final_args"] == RESTART["args"]


async def test_edit_approval_executes_edited_args(new_ticket, tenant):
    workflow.set_agent_for_tests(StubAgent(high_risk=RESTART))
    t = await new_ticket()
    intr = await _start(t, tenant)
    edited = {**RESTART["args"], "service": "vpn-gateway-standby"}
    await _resume(t, tenant, intr, await _approve_all(t, tenant, intr, "edit", edited))
    ops = await ticket_repo.list_ops_actions(t["id"], tenant)
    assert ops[0]["args"]["service"] == "vpn-gateway-standby"
    assert (await ticket_repo.list_approvals(tenant, ticket_id=t["id"]))[0]["status"] == "edited"


async def test_reject_approval_does_not_execute(new_ticket, tenant):
    workflow.set_agent_for_tests(StubAgent(high_risk=RESTART))
    t = await new_ticket()
    intr = await _start(t, tenant)
    intr = await _resume(t, tenant, intr, await _approve_all(t, tenant, intr, "reject"))
    assert intr["kind"] == "human_resolve"
    assert await ticket_repo.list_ops_actions(t["id"], tenant) == []
    assert (await ticket_repo.require_ticket(t["id"], tenant))["status"] == "ESCALATED"


async def test_decisions_must_cover_all_pending(new_ticket, tenant):
    workflow.set_agent_for_tests(StubAgent(high_risk=RESTART))
    t = await new_ticket()
    intr = await _start(t, tenant)
    with pytest.raises(approvals.ApprovalError):
        await approvals.decide(t["id"], tenant, intr["id"], [], None, "ops")


async def test_user_rejects_once_then_reprocess(new_ticket, tenant):
    stub = StubAgent()
    workflow.set_agent_for_tests(stub)
    t = await new_ticket()
    intr = await _start(t, tenant)
    intr = await _resume(t, tenant, intr, {"resolved": False, "feedback": "还是连不上"})
    assert intr["kind"] == "user_confirm" and stub.calls == 2
    ticket = await ticket_repo.require_ticket(t["id"], tenant)
    assert ticket["reopen_count"] == 1

    intr = await _resume(t, tenant, intr, {"resolved": False, "feedback": "依旧不行"})
    assert intr["kind"] == "human_resolve"
    ticket = await ticket_repo.require_ticket(t["id"], tenant)
    assert ticket["status"] == "ESCALATED" and not ticket["auto_resolved"] and stub.calls == 2


async def test_fallback_level2_attaches_references(new_ticket, tenant, monkeypatch):
    async def fake_search(query, tenant_id, doc_type=None):
        return {"docs": [{"content": "重新下载证书", "score": 0.9, "source_name": "VPN 手册"}],
                "fallback_used": False, "confidence": 0.9}
    monkeypatch.setattr(workflow, "search_knowledge", fake_search)
    workflow.set_agent_for_tests(StubAgent(error=RuntimeError("LLM down")))
    t = await new_ticket()
    intr = await _start(t, tenant)
    assert intr["kind"] == "human_resolve"
    ticket = await ticket_repo.require_ticket(t["id"], tenant)
    assert ticket["fallback_used"] and ticket["resolution"]["references"][0]["source"] == "VPN 手册"
    assert "参考方案" in ticket["escalation_reason"]


async def test_fallback_level3_when_retrieval_also_down(new_ticket, tenant, monkeypatch):
    async def dead_search(query, tenant_id, doc_type=None):
        return {"docs": [], "fallback_used": True, "confidence": 0.0}
    monkeypatch.setattr(workflow, "search_knowledge", dead_search)
    workflow.set_agent_for_tests(StubAgent(error=RuntimeError("LLM down")))
    t = await new_ticket()
    intr = await _start(t, tenant)
    assert intr["kind"] == "human_resolve"
    ticket = await ticket_repo.require_ticket(t["id"], tenant)
    assert ticket["fallback_used"] and "系统降级" in ticket["escalation_reason"]
    stats = await ticket_repo.stats(tenant)
    assert stats["fallback_rate"] > 0


async def test_sla_escalation_during_processing_is_kept(new_ticket, tenant):
    async def breach(ticket_id, tenant_id):
        await ticket_repo.update_fields(ticket_id, tenant_id,
                                        sla_due_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        await sla.scan_once()

    workflow.set_agent_for_tests(StubAgent(hook=breach))
    t = await new_ticket()
    intr = await _start(t, tenant)
    assert intr["kind"] == "human_resolve"
    ticket = await ticket_repo.require_ticket(t["id"], tenant)
    assert ticket["status"] == "ESCALATED" and "SLA" in ticket["escalation_reason"]


async def test_concurrent_run_is_rejected(new_ticket, tenant):
    from backend.services.locks import LockBusyError, distributed_lock
    workflow.set_agent_for_tests(StubAgent())
    t = await new_ticket()
    async with distributed_lock(f"ticket:{t['id']}"):
        with pytest.raises(LockBusyError):
            await _start(t, tenant)


async def test_progress_events_published(new_ticket, tenant):
    workflow.set_agent_for_tests(StubAgent())
    t = await new_ticket()
    await _start(t, tenant)
    events = [e async for e in ticket_runner.subscribe(t["id"])]
    types = [e["type"] for e in events]
    assert "stage" in types and types[-1] == "done"
    statuses = [e["status"] for e in events if e["type"] == "status"]
    assert statuses[0] == "NEW" and "PENDING_CONFIRM" in statuses


@pytest.mark.usefixtures("tenants")
async def test_checkpoint_survives_restart(new_ticket, tenant):
    """PostgreSQL Checkpoint：等审批时"重启服务"（关闭连接池、重建流程图），仍能从断点恢复并完成。"""
    from backend.core import checkpointer
    try:
        await checkpointer.close_checkpointer()
        await checkpointer.init_checkpointer()
    except Exception as e:
        pytest.skip(f"PostgreSQL Checkpoint 不可用：{e}")
    try:
        workflow.reset_workflow_for_tests()
        workflow.set_agent_for_tests(StubAgent(high_risk=RESTART))
        t = await new_ticket()
        intr = await _start(t, tenant)
        assert intr["kind"] == "approval"

        await checkpointer.close_checkpointer()          # —— 模拟进程重启 ——
        workflow.reset_workflow_for_tests()
        workflow.set_agent_for_tests(StubAgent(high_risk=RESTART))
        await checkpointer.init_checkpointer()

        restored = await ticket_runner.pending_interrupt(t["id"], tenant)
        assert restored["id"] == intr["id"] and restored["kind"] == "approval"
        intr = await _resume(t, tenant, restored, await _approve_all(t, tenant, restored))
        assert intr["kind"] == "user_confirm"
        assert [o["tool_name"] for o in await ticket_repo.list_ops_actions(t["id"], tenant)] == ["restart_service"]
    finally:
        await checkpointer.close_checkpointer()
