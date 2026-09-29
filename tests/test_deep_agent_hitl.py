"""真实 Deep Agent（create_deep_agent + interrupt_on）的高危审批：模型由脚本驱动，其余全部是生产代码。"""
import pytest
from langchain_core.messages import AIMessage
from langgraph.types import Command

from backend.agents.ticket import agent as agent_module
from backend.agents.ticket import workflow
from backend.agents.ticket.tools import HIGH_RISK_TOOLS
from backend.services import approvals, ticket_repo, ticket_runner
from tests.stubs import ScriptedToolModel, tool_call

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("memory_workflow")]

RESTART_ARGS = {"host": "vpn-gw-01", "service": "vpn-gateway", "reason": "地址池耗尽"}


def _script(high_risk_tool="restart_service", args=RESTART_ARGS):
    return [
        tool_call("submit_triage", {"category": "network", "priority": "P2", "summary": "VPN 地址池耗尽",
                                    "reason": "多人受影响"}, "c1"),
        tool_call(high_risk_tool, args, "c2"),
        tool_call("submit_resolution", {"resolved": True, "root_cause": "地址池耗尽", "summary": "已重启网关",
                                        "steps_taken": ["重启 vpn-gateway"], "user_actions": [],
                                        "confidence": 0.9}, "c3"),
        AIMessage(content="已处理完成"),
    ]


@pytest.fixture
def deep_agent(monkeypatch):
    def build(script):
        model = ScriptedToolModel(script=script)
        monkeypatch.setattr(agent_module, "get_llm", lambda *a, **k: model)
        agent = agent_module.build_ticket_agent()
        workflow.set_agent_for_tests(agent)
        return model
    return build


async def _run(ticket, tenant, graph_input=None):
    if graph_input is None:
        await ticket_repo.record_retrieval(ticket["id"], tenant, "预置检索", "runbook",
                                           {"strategy": "PRECISE", "docs": [], "confidence": 0.95})
    return await ticket_runner.run_to_pause(
        ticket["id"], tenant, graph_input or {"ticket_id": ticket["id"], "tenant_id": tenant, "attempt": 0})


async def _decide(ticket, tenant, intr, kind, args=None):
    pending = await ticket_repo.list_approvals(tenant, "pending", ticket["id"])
    value = await approvals.decide(ticket["id"], tenant, intr["id"],
                                   [approvals.Decision(a["id"], kind, args, "测试") for a in pending], None, "ops")
    return await _run(ticket, tenant, Command(resume={intr["id"]: value}))


def test_all_high_risk_tools_are_gated():
    assert set(HIGH_RISK_TOOLS) == {"restart_service", "reset_password", "grant_permission"}


async def test_deep_agent_pauses_before_high_risk_tool(deep_agent, new_ticket, tenant):
    deep_agent(_script())
    t = await new_ticket()
    intr = await _run(t, tenant)
    assert intr["kind"] == "approval"
    req = intr["value"]["action_requests"][0]
    assert req["name"] == "restart_service" and req["args"] == RESTART_ARGS
    assert intr["value"]["review_configs"][0]["allowed_decisions"] == ["approve", "edit", "reject"]
    assert await ticket_repo.list_ops_actions(t["id"], tenant) == []
    assert (await ticket_repo.require_ticket(t["id"], tenant))["status"] == "PENDING_APPROVAL"


async def test_deep_agent_approve(deep_agent, new_ticket, tenant):
    deep_agent(_script())
    t = await new_ticket()
    intr = await _decide(t, tenant, await _run(t, tenant), "approve")
    assert intr["kind"] == "user_confirm"
    ops = await ticket_repo.list_ops_actions(t["id"], tenant)
    assert [(o["tool_name"], o["args"]) for o in ops] == [("restart_service", RESTART_ARGS)]


async def test_deep_agent_edit(deep_agent, new_ticket, tenant):
    deep_agent(_script())
    t = await new_ticket()
    edited = {**RESTART_ARGS, "reason": "运维修改：夜间窗口执行"}
    await _decide(t, tenant, await _run(t, tenant), "edit", edited)
    ops = await ticket_repo.list_ops_actions(t["id"], tenant)
    assert ops[0]["args"]["reason"] == "运维修改：夜间窗口执行"


async def test_deep_agent_reject_never_executes(deep_agent, new_ticket, tenant):
    model = deep_agent(_script())
    t = await new_ticket()
    await _decide(t, tenant, await _run(t, tenant), "reject")
    assert await ticket_repo.list_ops_actions(t["id"], tenant) == []
    audit = (await ticket_repo.list_approvals(tenant, ticket_id=t["id"]))[0]
    assert audit["status"] == "rejected" and audit["final_args"] is None
    assert model.cursor >= 3


async def test_low_risk_tool_runs_without_approval(deep_agent, new_ticket, tenant):
    deep_agent(_script("unlock_account", {"username": "zhangsan"}))
    t = await new_ticket(title="账号锁定", description="登录提示账户已锁定", host="dc01")
    intr = await _run(t, tenant)
    assert intr["kind"] == "user_confirm"
    assert await ticket_repo.list_approvals(tenant, ticket_id=t["id"]) == []
    assert [o["tool_name"] for o in await ticket_repo.list_ops_actions(t["id"], tenant)] == ["unlock_account"]


async def test_skills_are_readable_but_not_writable():
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
    backend = CompositeBackend(default=StateBackend(), routes={
        agent_module.SKILLS_ROUTE: FilesystemBackend(root_dir=agent_module.SKILLS_DIR, virtual_mode=True)})
    names = {e["path"].rstrip("/").split("/")[-1] for e in backend.ls("/skills/").entries}
    assert names == {"password-reset", "vpn-troubleshooting", "printer-issues", "email-issues", "permission-request"}
    for d in names:
        text = (agent_module.SKILLS_DIR / d / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---\nname: " + d)
