"""清空演示租户里的工单（含流转、审批、检索、运维记录、运行埋点和 LangGraph checkpoint），用户和知识库保留。

    python scripts/reset_demo_tickets.py          # 只统计
    python scripts/reset_demo_tickets.py --yes    # 真正删除
"""
import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from sqlalchemy import text  # noqa: E402

from backend.dependencies import AsyncSessionLocal  # noqa: E402
from data.scenarios import TENANTS  # noqa: E402

CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args()
    tenants = list(TENANTS)
    async with AsyncSessionLocal() as db, db.begin():
        rows = (await db.execute(
            text("SELECT tenant_id, status, COUNT(*) FROM tickets WHERE tenant_id = ANY(:t) "
                 "GROUP BY tenant_id, status ORDER BY 1, 2"), {"t": tenants})).fetchall()
        for r in rows:
            print(f"{r[0]:<10} {r[1]:<17} {r[2]}")
        if not args.yes:
            print("未删除；加 --yes 执行")
            return
        ids = [str(i) for (i,) in (await db.execute(
            text("SELECT id FROM tickets WHERE tenant_id = ANY(:t)"), {"t": tenants})).fetchall()]
        threads = [f"ticket-{i}" for i in ids]
        existing = {n for (n,) in (await db.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).fetchall()}
        for table in CHECKPOINT_TABLES:
            if table in existing:
                await db.execute(text(f"DELETE FROM {table} WHERE thread_id = ANY(:th)"), {"th": threads})
        await db.execute(text("DELETE FROM ticket_runs WHERE tenant_id = ANY(:t)"), {"t": tenants})
        await db.execute(text("DELETE FROM tickets WHERE tenant_id = ANY(:t)"), {"t": tenants})
        print(f"已删除 {len(ids)} 张工单")


if __name__ == "__main__":
    asyncio.run(main())
