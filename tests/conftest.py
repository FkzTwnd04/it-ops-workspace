"""测试公共夹具。

数据库测试直连本地 PostgreSQL（docker compose up -d postgres），每次测试会话使用独立的随机租户，
结束后级联删除；不依赖 Milvus 和大模型：检索、Agent 在需要的地方用桩替换。
"""
import asyncio
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    # psycopg 异步连接（PostgreSQL Checkpoint）不支持 Windows 默认的 Proactor 循环
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="session")
def event_loop_policy():
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest_asyncio.fixture(scope="session")
async def db_ready():
    from backend.dependencies import AsyncSessionLocal
    from backend.db.migrations import run_migrations
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"PostgreSQL 不可用：{e}")
    await run_migrations()
    yield


@pytest_asyncio.fixture(scope="session")
async def tenants(db_ready):
    """两个随机测试租户，会话结束后清理。"""
    from backend.dependencies import AsyncSessionLocal
    suffix = uuid.uuid4().hex[:8]
    ids = (f"t_test_a_{suffix}", f"t_test_b_{suffix}")
    yield ids
    async with AsyncSessionLocal() as db, db.begin():
        for t in ids:
            await db.execute(text("DELETE FROM tickets WHERE tenant_id = :t"), {"t": t})
            await db.execute(text("DELETE FROM ticket_runs WHERE tenant_id = :t"), {"t": t})
            await db.execute(text("DELETE FROM users WHERE tenant_id = :t"), {"t": t})


@pytest.fixture
def tenant(tenants):
    return tenants[0]


@pytest.fixture
def other_tenant(tenants):
    return tenants[1]


async def _make_user(tenant_id: str, role: str, name: str) -> dict:
    from backend.dependencies import AsyncSessionLocal
    uid = str(uuid.uuid4())
    username = f"{name}_{uid[:6]}"
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            text("INSERT INTO users (id, tenant_id, username, email, password_hash, role, display_name) "
                 "VALUES (:id, :t, :u, :e, 'x', :r, :n)"),
            {"id": uid, "t": tenant_id, "u": username, "e": f"{username}@test.local", "r": role, "n": name},
        )
    return {"user_id": uid, "tenant_id": tenant_id, "role": role, "name": name, "username": username}


@pytest_asyncio.fixture
async def requester(tenant):
    return await _make_user(tenant, "requester", "req")


@pytest_asyncio.fixture
async def ops(tenant):
    return await _make_user(tenant, "ops", "ops")


@pytest_asyncio.fixture
async def make_user():
    return _make_user


@pytest_asyncio.fixture
async def new_ticket(tenant, requester):
    from backend.services import ticket_repo

    async def factory(title="VPN 连不上", description="提示证书验证失败", host="vpn-gw-01", tenant_id=None):
        return await ticket_repo.create_ticket(tenant_id or tenant, title, description,
                                               requester["user_id"], requester["name"], host)
    return factory


@pytest.fixture
def memory_workflow():
    """把工单流程图切到 MemorySaver；测试结束恢复。"""
    from langgraph.checkpoint.memory import MemorySaver

    from backend.agents.ticket import workflow
    from backend.core import checkpointer
    checkpointer.set_checkpointer(MemorySaver())
    workflow.reset_workflow_for_tests()
    yield
    workflow.set_agent_for_tests(None)
    workflow.reset_workflow_for_tests()
    checkpointer.set_checkpointer(None)
