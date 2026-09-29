"""列出同名账号（用户名或邮箱重复会导致登录匹配到错误的用户）。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import text  # noqa: E402

from backend.dependencies import AsyncSessionLocal  # noqa: E402


async def main(names: list[str]) -> None:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text("SELECT username, email, tenant_id, role, is_active, created_at FROM users "
                 "WHERE username = ANY(:n) OR split_part(email, '@', 1) = ANY(:n) ORDER BY username, created_at"),
            {"n": names})).fetchall()
    for r in rows:
        print(r.username, r.email, r.tenant_id, r.role, r.is_active, r.created_at)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or ["admin"]))
