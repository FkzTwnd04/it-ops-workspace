"""HTTP 接口：权限、租户隔离、完整处理流程、SSE 进度、审批台、统计。"""
import asyncio
import json

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from backend.agents.ticket import workflow
from backend.api.router import api_router
from backend.api.v1.auth import _create_access_token
from backend.services import ticket_repo, ticket_runner
from tests.stubs import StubAgent

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("memory_workflow")]

RESTART = {"name": "restart_service", "args": {"host": "mail01", "service": "postfix", "reason": "队列积压"}}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    return app


def _headers(user: dict) -> dict:
    token = _create_access_token({"sub": user["user_id"], "role": user["role"], "tenant_id": user["tenant_id"],
                                  "name": user["name"]}, 30)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://test") as c:
        yield c


async def _wait_idle(ticket_id: str, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if not ticket_runner.is_running(ticket_id):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("工单处理超时")


async def _create(client, user, **kw):
    body = {"title": "邮件发不出去", "description": "外发邮件一直在发件箱", "host": "mail01", "auto_run": True, **kw}
    r = await client.post("/api/v1/tickets", json=body, headers=_headers(user))
    assert r.status_code == 201, r.text
    t = r.json()
    await _wait_idle(t["id"])
    return t


async def test_login_same_username_in_two_tenants(client, tenant, other_tenant):
    import uuid
    from sqlalchemy import text
    from backend.api.v1.auth import pwd_context
    from backend.dependencies import AsyncSessionLocal
    name = f"dup_{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as db, db.begin():
        for t, pw in ((tenant, "old-pass"), (other_tenant, "Passw0rd!")):
            await db.execute(text("INSERT INTO users (id, tenant_id, username, email, password_hash, role) "
                                  "VALUES (:id, :t, :u, :e, :h, 'ops')"),
                             {"id": str(uuid.uuid4()), "t": t, "u": name, "e": f"{name}@{t}.local",
                              "h": pwd_context.hash(pw)})
    for pw, expected_tenant in (("Passw0rd!", other_tenant), ("old-pass", tenant)):
        r = await client.post("/api/v1/auth/login", json={"username": name, "password": pw})
        assert r.status_code == 200 and r.json()["tenant_id"] == expected_tenant
    assert (await client.post("/api/v1/auth/login", json={"username": name, "password": "x"})).status_code == 401


async def test_requires_token(client):
    assert (await client.get("/api/v1/tickets")).status_code in (401, 403)


async def test_full_flow_create_run_confirm(client, requester):
    workflow.set_agent_for_tests(StubAgent())
    t = await _create(client, requester)
    detail = (await client.get(f"/api/v1/tickets/{t['id']}", headers=_headers(requester))).json()
    assert detail["ticket"]["status"] == "PENDING_CONFIRM" and detail["pending"] == "user_confirm"
    assert detail["retrievals"] and detail["events"]

    r = await client.post(f"/api/v1/tickets/{t['id']}/confirm", json={"resolved": True}, headers=_headers(requester))
    assert r.status_code == 202
    await _wait_idle(t["id"])
    detail = (await client.get(f"/api/v1/tickets/{t['id']}", headers=_headers(requester))).json()
    assert detail["ticket"]["status"] == "CLOSED" and detail["pending"] is None


async def test_sse_stream_ends_with_done(client, requester):
    workflow.set_agent_for_tests(StubAgent())
    t = await _create(client, requester)
    events = []
    async with client.stream("GET", f"/api/v1/tickets/{t['id']}/stream", headers=_headers(requester)) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    assert events[-1]["type"] == "done" and events[-1]["pending"] == "user_confirm"
    assert any(e["type"] == "status" and e["status"] == "PENDING_CONFIRM" for e in events)


async def test_requester_isolation(client, requester, make_user, tenant):
    workflow.set_agent_for_tests(StubAgent())
    t = await _create(client, requester, auto_run=False)
    other = await make_user(tenant, "requester", "other")
    assert (await client.get(f"/api/v1/tickets/{t['id']}", headers=_headers(other))).status_code == 404
    listed = (await client.get("/api/v1/tickets", headers=_headers(other))).json()
    assert all(x["id"] != t["id"] for x in listed)


async def test_cross_tenant_ops_cannot_see(client, requester, make_user, other_tenant):
    workflow.set_agent_for_tests(StubAgent())
    t = await _create(client, requester, auto_run=False)
    foreign_ops = await make_user(other_tenant, "ops", "ops_b")
    assert (await client.get(f"/api/v1/tickets/{t['id']}", headers=_headers(foreign_ops))).status_code == 404
    r = await client.post(f"/api/v1/tickets/{t['id']}/resolve", json={"action": "resolve", "summary": "x"},
                          headers=_headers(foreign_ops))
    assert r.status_code == 404


async def test_requester_cannot_use_ops_endpoints(client, requester):
    workflow.set_agent_for_tests(StubAgent())
    t = await _create(client, requester, auto_run=False)
    h = _headers(requester)
    assert (await client.get("/api/v1/approvals", headers=h)).status_code == 403
    assert (await client.get("/api/v1/stats", headers=h)).status_code == 403
    assert (await client.post(f"/api/v1/tickets/{t['id']}/approvals", json={"decisions": []},
                              headers=h)).status_code == 403


async def test_approval_console_flow(client, requester, ops):
    workflow.set_agent_for_tests(StubAgent(high_risk=RESTART))
    t = await _create(client, requester)
    pending = (await client.get("/api/v1/approvals", headers=_headers(ops))).json()
    mine = [a for a in pending if a["ticket_id"] == t["id"]]
    assert len(mine) == 1 and mine[0]["tool_name"] == "restart_service" and mine[0]["risk_note"]

    bad = await client.post(f"/api/v1/tickets/{t['id']}/approvals", json={"decisions": []}, headers=_headers(ops))
    assert bad.status_code == 422

    r = await client.post(f"/api/v1/tickets/{t['id']}/approvals", headers=_headers(ops), json={
        "decisions": [{"approval_id": mine[0]["id"], "type": "approve", "comment": "同意"}]})
    assert r.status_code == 202
    await _wait_idle(t["id"])
    detail = (await client.get(f"/api/v1/tickets/{t['id']}", headers=_headers(ops))).json()
    assert detail["ticket"]["status"] == "PENDING_CONFIRM"
    assert [o["tool_name"] for o in detail["ops_actions"]] == ["restart_service"]
    assert detail["approvals"][0]["status"] == "approved" and detail["approvals"][0]["comment"] == "同意"

    again = await client.post(f"/api/v1/tickets/{t['id']}/approvals", headers=_headers(ops), json={
        "decisions": [{"approval_id": mine[0]["id"], "type": "approve"}]})
    assert again.status_code == 409


async def test_ops_resolves_escalated(client, requester, ops):
    workflow.set_agent_for_tests(StubAgent(resolved=False))
    t = await _create(client, requester)
    h = _headers(ops)
    assert (await client.post(f"/api/v1/tickets/{t['id']}/resolve", json={"action": "resolve"},
                              headers=h)).status_code == 422
    r = await client.post(f"/api/v1/tickets/{t['id']}/resolve", headers=h,
                          json={"action": "resolve", "summary": "更换邮件网关证书"})
    assert r.status_code == 202
    await _wait_idle(t["id"])
    assert (await ticket_repo.require_ticket(t["id"], requester["tenant_id"]))["status"] == "CLOSED"


async def test_run_twice_conflicts(client, requester):
    workflow.set_agent_for_tests(StubAgent())
    t = await _create(client, requester)
    r = await client.post(f"/api/v1/tickets/{t['id']}/run", headers=_headers(requester))
    assert r.status_code == 409


async def test_stats_for_ops(client, requester, ops):
    workflow.set_agent_for_tests(StubAgent())
    await _create(client, requester)
    s = (await client.get("/api/v1/stats", headers=_headers(ops))).json()
    for key in ("auto_resolve_rate", "escalation_rate", "approval_rate", "fallback_rate",
                "latency_p50_ms", "latency_p95_ms", "by_status"):
        assert key in s
    assert s["runs"] >= 1
