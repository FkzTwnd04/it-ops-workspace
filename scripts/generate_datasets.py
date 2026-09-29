"""生成模拟数据：200 条历史工单（入知识库）+ 100 条评测工单（评测脚本用）。

所有数据都是按 data/scenarios.py 模板生成的模拟数据，固定随机种子，重复运行结果一致。
    python scripts/generate_datasets.py
"""
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data.scenarios import HOLDOUT_COUNT, SCENARIOS, TENANTS, USER_NAMES, USERS  # noqa: E402

DATA = ROOT / "data"
EVAL_SIZE = 100
EVAL_BRANCH = 10

_PREFIX = ["", "你好，", "麻烦看一下，", "IT 同事你好，", "急！", ""]
_SUFFIX = ["", "谢谢。", "麻烦尽快处理。", "今天上午必须要用。", "", "已经重启过电脑了。"]


def _history(rng: random.Random) -> list[dict]:
    rows, start = [], datetime(2026, 3, 1, 9, 0)
    pool = [(s, p) for s in SCENARIOS for p in s["phrasings"][:-HOLDOUT_COUNT]]
    n = 0
    for tenant, cfg in TENANTS.items():
        for _ in range(cfg["history"]):
            s, (title, desc) = rng.choice(pool)
            user = rng.choice(USERS)
            created = start + timedelta(hours=rng.randint(0, 24 * 150))
            n += 1
            rows.append({
                "ticket_no": f"INC{created:%Y%m%d}H{n:04d}",
                "tenant_id": tenant,
                "scenario": s["key"],
                "category": s["category"],
                "priority": s["priority"],
                "title": title,
                "description": desc,
                "host": s["host"],
                "requester": USER_NAMES[user],
                "root_cause": s["root_cause"],
                "resolution": s["resolution"],
                "outcome": "人工处理" if s["expected"] == "escalate" else "已解决",
                "created_at": created.isoformat(),
                "simulated": True,
            })
    return rows


def _eval(rng: random.Random) -> list[dict]:
    rows = []
    per = [(s, p) for s in SCENARIOS for p in s["phrasings"][-HOLDOUT_COUNT:]]
    for i in range(EVAL_SIZE):
        s, (title, desc) = per[i % len(per)]
        user = USERS[i % len(USERS)]
        rows.append({
            "case_id": f"EVAL-{i + 1:03d}",
            "tenant_id": "it_branch" if i >= EVAL_SIZE - EVAL_BRANCH else "it_hq",
            "scenario": s["key"],
            "title": title,
            "description": f"{rng.choice(_PREFIX)}{desc}。{rng.choice(_SUFFIX)}".replace("。。", "。"),
            "host": s["host"],
            "requester_username": user,
            "expected_category": s["category"],
            "expected_priority": s["priority"],
            "expected_outcome": s["expected"],
            "expected_tool": s["tool"],
            "reference": f"根因：{s['root_cause']}。处理：{s['resolution']}。",
            "simulated": True,
        })
    return rows


def main() -> None:
    rng = random.Random(20260925)
    history = _history(rng)
    evals = _eval(rng)
    for name, rows in (("history_tickets.jsonl", history), ("eval_tickets.jsonl", evals)):
        with open(DATA / name, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"{name}: {len(rows)} 条")
    auto = sum(r["expected_outcome"] == "auto" for r in evals)
    print(f"评测集期望自动解决 {auto}/{len(evals)}，期望转人工 {len(evals) - auto}/{len(evals)}")


if __name__ == "__main__":
    main()
