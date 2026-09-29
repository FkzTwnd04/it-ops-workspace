# scripts/seed_data.py
# 执行：python scripts/seed_data.py
# 用途：灌入演示账号。两个租户（集团总部 it_hq / 深圳分公司 it_branch）用来演示数据隔离。

import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
import bcrypt as _b  # noqa: E402
import types as _t  # noqa: E402
from passlib.context import CryptContext  # noqa: E402

from backend.config import get_settings  # noqa: E402
from data.scenarios import USER_NAMES, USERS  # noqa: E402

if not hasattr(_b, "__about__"):
    _b.__about__ = _t.SimpleNamespace(__version__=getattr(_b, "__version__", "4.x"))

DEMO_PASSWORD = "Passw0rd!"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def demo_users() -> list[dict]:
    users = [
        {"tenant": "it_hq", "username": "admin", "name": "系统管理员", "role": "admin"},
        {"tenant": "it_hq", "username": "ops_wang", "name": "王工（总部运维）", "role": "ops"},
        {"tenant": "it_branch", "username": "ops_chen", "name": "陈工（深圳运维）", "role": "ops"},
    ]
    for u in USERS:
        users.append({"tenant": "it_hq", "username": u, "name": USER_NAMES[u], "role": "requester"})
        users.append({"tenant": "it_branch", "username": f"sz_{u}", "name": f"{USER_NAMES[u]}（深圳）",
                      "role": "requester"})
    return users


async def seed_users() -> None:
    s = get_settings()
    conn = await asyncpg.connect(f"postgresql://{s.db_user}:{s.db_password}@{s.db_host}:{s.db_port}/{s.db_name}")
    try:
        pwd_hash = pwd_context.hash(DEMO_PASSWORD)
        users = demo_users()
        for u in users:
            await conn.execute(
                """
                INSERT INTO users (id, tenant_id, username, email, password_hash, role, display_name)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (tenant_id, email) DO UPDATE
                SET role = EXCLUDED.role, display_name = EXCLUDED.display_name
                """,
                str(uuid.uuid4()), u["tenant"], u["username"], f"{u['username']}@corp.local",
                pwd_hash, u["role"], u["name"],
            )
        print(f"演示账号 {len(users)} 个，密码均为 {DEMO_PASSWORD}")
        print("  总部：admin / ops_wang（运维）/ zhangsan 等（提单人）")
        print("  深圳：ops_chen（运维）/ sz_zhangsan 等（提单人）")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(seed_users())
