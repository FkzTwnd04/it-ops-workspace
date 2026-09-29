"""工单评测：把评测集逐条当作真实工单跑完整流程，统计自动解决率、准确率、延迟、降级率等指标。

流程与线上完全一致（同一个工作流、同一个 Deep Agent、同一套落库逻辑），只是人工环节由脚本模拟：
  - 高危审批：模拟值班运维，默认全部批准；申请 admin 级权限时拒绝
  - 用户确认：模拟提单人，期望自动解决的用例回答"已解决"，期望转人工的回答"未解决"（触发重新处理）
  - 人工处理：模拟运维直接结案

    python scripts/eval_tickets.py                 # 全量 100 条
    python scripts/eval_tickets.py --limit 5       # 冒烟
    python scripts/eval_tickets.py --concurrency 4
结果写入 data/eval_results/<时间>.json，指标同时打印到控制台。
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langgraph.types import Command  # noqa: E402
from sqlalchemy import text  # noqa: E402

from backend.core.logger import configure_logging  # noqa: E402
from backend.dependencies import AsyncSessionLocal  # noqa: E402
from backend.services import approvals, ticket_repo, ticket_runner  # noqa: E402

RESULTS = ROOT / "data" / "eval_results"
MAX_STEPS = 8


async def _users() -> dict[tuple[str, str], dict]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(text("SELECT id, tenant_id, username, display_name FROM users"))).fetchall()
    return {(r.tenant_id, r.username): {"id": str(r.id), "name": r.display_name or r.username} for r in rows}


def _requester(case: dict, users: dict) -> dict:
    name = case["requester_username"]
    if case["tenant_id"] == "it_branch":
        name = f"sz_{name}"
    return users[(case["tenant_id"], name)]


async def run_case(case: dict, users: dict) -> dict:
    tenant = case["tenant_id"]
    req = _requester(case, users)
    ticket = await ticket_repo.create_ticket(tenant, case["title"], case["description"], req["id"],
                                             req["name"], case["host"] or None)
    tid = ticket["id"]
    started = time.perf_counter()
    trace, outcome, error = [], None, None
    try:
        intr = await ticket_runner.run_to_pause(tid, tenant, {"ticket_id": tid, "tenant_id": tenant, "attempt": 0})
        for _ in range(MAX_STEPS):
            if intr is None:
                break
            trace.append(intr["kind"])
            if intr["kind"] == "approval":
                pending = [a for a in await ticket_repo.list_approvals(tenant, "pending", tid)
                           if a["interrupt_id"] == intr["id"]]
                decisions = [
                    approvals.Decision(a["id"], "reject", comment="admin 权限需安全组评估")
                    if a["tool_name"] == "grant_permission" and (a["tool_args"] or {}).get("level") == "admin"
                    else approvals.Decision(a["id"], "approve")
                    for a in pending
                ]
                value = await approvals.decide(tid, tenant, intr["id"], decisions, None, "eval-ops")
            elif intr["kind"] == "user_confirm":
                ok = case["expected_outcome"] == "auto"
                outcome = outcome or ("auto" if ok else "auto_rejected_by_user")
                value = {"resolved": ok, "feedback": "" if ok else "问题没有解决", "actor": req["name"]}
            else:
                outcome = "escalate" if outcome in (None, "auto_rejected_by_user") else outcome
                value = {"action": "resolve", "summary": "评测：人工处理完成", "actor": "eval-ops"}
            intr = await ticket_runner.run_to_pause(tid, tenant, Command(resume={intr["id"]: value}))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    final = await ticket_repo.require_ticket(tid, tenant)
    ops = await ticket_repo.list_ops_actions(tid, tenant)
    appr = await ticket_repo.list_approvals(tenant, ticket_id=tid)
    return {
        "case_id": case["case_id"],
        "scenario": case["scenario"],
        "tenant_id": tenant,
        "ticket_id": tid,
        "ticket_no": final["ticket_no"],
        "expected_outcome": case["expected_outcome"],
        "outcome": "escalate" if outcome == "auto_rejected_by_user" else outcome,
        "first_pass": outcome,
        "final_status": final["status"],
        "expected_category": case["expected_category"],
        "category": final["category"],
        "expected_priority": case["expected_priority"],
        "priority": final["priority"],
        "expected_tool": case["expected_tool"],
        "tools_executed": [o["tool_name"] for o in ops],
        "approvals": [{"tool": a["tool_name"], "status": a["status"]} for a in appr],
        "retrieval_confidence": final["retrieval_confidence"],
        "resolution_confidence": (final["resolution"] or {}).get("confidence"),
        "fallback_used": final["fallback_used"],
        "escalation_reason": final["escalation_reason"],
        "trace": trace,
        "wall_seconds": round(time.perf_counter() - started, 2),
        "error": error,
    }


def _rate(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 1)


async def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    ids = [r["ticket_id"] for r in rows]
    async with AsyncSessionLocal() as db:
        runs = (await db.execute(
            text("SELECT latency_ms, fallback_level FROM ticket_runs "
                 "WHERE ticket_id = ANY(CAST(:ids AS UUID[])) AND finished_at IS NOT NULL"),
            {"ids": ids},
        )).fetchall()
    latencies = [r.latency_ms / 1000 for r in runs if r.latency_ms is not None]
    auto = [r for r in rows if r["outcome"] == "auto"]
    exp_auto = [r for r in rows if r["expected_outcome"] == "auto"]
    exp_esc = [r for r in rows if r["expected_outcome"] == "escalate"]
    with_tool = [r for r in rows if r["expected_tool"]]
    return {
        "cases": n,
        "errors": sum(1 for r in rows if r["error"]),
        "auto_resolve_rate": _rate(len(auto), n),
        "human_intervention_rate": _rate(sum(1 for r in rows if r["outcome"] == "escalate"), n),
        "approval_rate": _rate(sum(1 for r in rows if r["approvals"]), n),
        "outcome_accuracy": _rate(sum(1 for r in rows if r["outcome"] == r["expected_outcome"]), n),
        "auto_precision": _rate(sum(1 for r in auto if r["expected_outcome"] == "auto"), len(auto)),
        "auto_recall": _rate(sum(1 for r in exp_auto if r["outcome"] == "auto"), len(exp_auto)),
        "escalate_recall": _rate(sum(1 for r in exp_esc if r["outcome"] == "escalate"), len(exp_esc)),
        "category_accuracy": _rate(sum(1 for r in rows if r["category"] == r["expected_category"]), n),
        "priority_accuracy": _rate(sum(1 for r in rows if r["priority"] == r["expected_priority"]), n),
        "expected_tool_hit": _rate(sum(1 for r in with_tool if r["expected_tool"] in r["tools_executed"]),
                                   len(with_tool)),
        "runs": len(runs),
        "fallback_rate": _rate(sum(1 for r in runs if r.fallback_level > 0), len(runs)),
        "latency_p50_s": _pct(latencies, 0.5),
        "latency_p95_s": _pct(latencies, 0.95),
        "latency_mean_s": round(statistics.mean(latencies), 1) if latencies else None,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--cases", nargs="*", help="只跑指定 case_id")
    args = parser.parse_args()

    configure_logging()
    import logging
    logging.getLogger().setLevel(logging.WARNING)

    from backend.core.checkpointer import close_checkpointer, init_checkpointer
    from backend.db.migrations import run_migrations
    await run_migrations()
    await init_checkpointer()

    from backend.core.knowledge_base import BGEMEmbedder
    from backend.core.reranker import MODEL_EXECUTOR, BGEReranker
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(MODEL_EXECUTOR, BGEMEmbedder.get_instance)
    await loop.run_in_executor(MODEL_EXECUTOR, BGEReranker.get_instance)

    with open(ROOT / "data" / "eval_tickets.jsonl", encoding="utf-8") as f:
        cases = [json.loads(line) for line in f]
    if args.cases:
        cases = [c for c in cases if c["case_id"] in set(args.cases)]
    cases = cases[args.offset:]
    if args.limit:
        cases = cases[:args.limit]

    users = await _users()
    sem = asyncio.Semaphore(args.concurrency)
    rows: list[dict] = []

    async def worker(case):
        async with sem:
            r = await run_case(case, users)
            rows.append(r)
            print(f"[{len(rows):3d}/{len(cases)}] {r['case_id']} {r['scenario']:<18} expected={r['expected_outcome']:<8} "
                  f"got={r['outcome']} status={r['final_status']} cat={r['category']} "
                  f"tools={r['tools_executed']} {r['wall_seconds']}s {r['error'] or ''}", flush=True)

    await asyncio.gather(*(worker(c) for c in cases))
    summary = await summarize(rows)
    await close_checkpointer()

    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"eval_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps({"summary": summary, "rows": sorted(rows, key=lambda r: r["case_id"])},
                              ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"明细：{out}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
