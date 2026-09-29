"""手动检查：检索质量 + 租户隔离。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.agents.ticket.retrieval import search_knowledge

CASES = [
    ("it_hq", "VPN 提示证书验证失败", "runbook"),
    ("it_hq", "打印任务一直卡在队列里", "runbook"),
    ("it_hq", "ERP 页面一直转圈，财务结账受影响", "runbook"),
    ("it_hq", "账户已锁定无法登录", "ticket"),
    ("it_hq", "深圳分公司 VPN 网关地址", "runbook"),
    ("it_branch", "深圳分公司 VPN 网关地址", "runbook"),
    ("it_hq", "系统用不了了", "runbook"),
]


async def main():
    for tenant, q, dt in CASES:
        r = await search_knowledge(q, tenant, doc_type=dt)
        top = r["docs"][0]["source_name"] if r["docs"] else "-"
        print(f"[{tenant}] {q} | strategy={r['strategy']} conf={r['confidence']} low={r['low_confidence']} top={top}")


asyncio.run(main())
