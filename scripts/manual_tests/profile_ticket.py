"""手动剖析：跑一张工单，打印进度事件时间线（看时间花在哪一步）。

    python scripts/manual_tests/profile_ticket.py EVAL-003
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from langgraph.types import Command  # noqa: E402

from backend.services import approvals, ticket_repo, ticket_runner  # noqa: E402
from scripts.eval_tickets import _requester, _users  # noqa: E402


def _print_timeline(events: list[dict], t0: float) -> float:
    last = t0
    for ev in events:
        if ev["type"] in ("ping",):
            continue
        detail = {k: v for k, v in ev.items() if k not in ("ts", "type")}
        text = json.dumps(detail, ensure_ascii=False)[:150]
        print(f"{ev['ts'] - t0:6.1f}s (+{ev['ts'] - last:5.1f}) {ev['type']:<16} {text}")
        last = ev["ts"]
    return last


async def main(case_id: str):
    from backend.core.checkpointer import init_checkpointer
    from backend.core.knowledge_base import BGEMEmbedder
    from backend.core.reranker import MODEL_EXECUTOR, BGEReranker
    await init_checkpointer()
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(MODEL_EXECUTOR, BGEMEmbedder.get_instance)
    await loop.run_in_executor(MODEL_EXECUTOR, BGEReranker.get_instance)

    case = next(json.loads(line) for line in open(ROOT / "data" / "eval_tickets.jsonl", encoding="utf-8")
                if json.loads(line)["case_id"] == case_id)
    req = _requester(case, await _users())
    t = await ticket_repo.create_ticket(case["tenant_id"], case["title"], case["description"], req["id"],
                                        req["name"], case["host"] or None)
    tid, tenant = t["id"], case["tenant_id"]
    print(f"{case_id} {case['title']} | {case['description']}")

    graph_input = {"ticket_id": tid, "tenant_id": tenant, "attempt": 0}
    t0 = None
    for _ in range(6):
        intr = await ticket_runner.run_to_pause(tid, tenant, graph_input)
        events = ticket_runner._history.get(tid, [])
        t0 = t0 or (events[0]["ts"] if events else 0)
        _print_timeline(events, t0)
        if not intr:
            break
        print(f"------ 中断：{intr['kind']}")
        if intr["kind"] == "approval":
            pending = [a for a in await ticket_repo.list_approvals(tenant, "pending", tid)
                       if a["interrupt_id"] == intr["id"]]
            value = await approvals.decide(tid, tenant, intr["id"],
                                           [approvals.Decision(a["id"], "approve") for a in pending], None, "ops")
        elif intr["kind"] == "user_confirm":
            value = {"resolved": True}
        else:
            break
        graph_input = Command(resume={intr["id"]: value})

    final = await ticket_repo.require_ticket(tid, tenant)
    print("最终状态:", final["status"], "| 结论:", json.dumps(final["resolution"], ensure_ascii=False)[:300])


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "EVAL-003"))
