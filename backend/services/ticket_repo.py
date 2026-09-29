# backend/services/ticket_repo.py
# 工单数据访问层。所有读写都必须带 tenant_id（SLA 巡检这种系统级扫描除外），
# 状态流转只能走 transition()：行锁 → 白名单校验 → 更新 → 写审计事件，在同一事务里完成。

import json
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from backend.agents.ticket.states import (
    Priority,
    TicketStatus,
    assert_transition,
    compute_sla_due,
)
from backend.core.logger import get_logger
from backend.dependencies import AsyncSessionLocal

logger = get_logger(__name__)

_JSON_COLUMNS = {"triage", "resolution"}
_UPDATABLE_COLUMNS = {
    "category", "priority", "sla_due_at", "escalation_reason", "triage", "resolution",
    "auto_resolved", "fallback_used", "resolved_at", "closed_at", "reopen_count",
}
_TICKET_COLUMNS = (
    "id, ticket_no, tenant_id, title, description, requester_id, requester_name, host, "
    "category, priority, status, sla_due_at, escalation_reason, triage, resolution, "
    "auto_resolved, fallback_used, retrieval_confidence, reopen_count, version, "
    "created_at, updated_at, resolved_at, closed_at"
)


class TicketNotFoundError(Exception):
    pass


def _row(r) -> dict[str, Any]:
    d = dict(r._mapping)
    for k in ("id", "requester_id", "ticket_id", "reviewer_id"):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d


def _set_clause(fields: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    unknown = set(fields) - _UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"不允许更新的字段: {sorted(unknown)}")
    parts, params = [], {}
    for k, v in fields.items():
        if k in _JSON_COLUMNS:
            parts.append(f"{k} = CAST(:f_{k} AS JSONB)")
            params[f"f_{k}"] = json.dumps(v, ensure_ascii=False) if v is not None else None
        else:
            parts.append(f"{k} = :f_{k}")
            params[f"f_{k}"] = v
    return ", ".join(parts), params


# ── 工单 ────────────────────────────────────────────────────

async def create_ticket(
    tenant_id: str,
    title: str,
    description: str,
    requester_id: Optional[str],
    requester_name: Optional[str],
    host: Optional[str] = None,
    priority: str = Priority.P3.value,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db, db.begin():
        seq = (await db.execute(text("SELECT nextval('ticket_no_seq')"))).scalar_one()
        ticket_no = f"INC{now:%Y%m%d}{seq:05d}"
        row = (await db.execute(
            text(f"""
                INSERT INTO tickets (ticket_no, tenant_id, title, description, requester_id,
                                     requester_name, host, priority, status, sla_due_at, created_at)
                VALUES (:no, :tenant, :title, :desc, :rid, :rname, :host, :prio, 'NEW', :due, :now)
                RETURNING {_TICKET_COLUMNS}
            """),
            {
                "no": ticket_no, "tenant": tenant_id, "title": title, "desc": description,
                "rid": requester_id, "rname": requester_name, "host": host,
                "prio": priority, "due": compute_sla_due(priority, now), "now": now,
            },
        )).fetchone()
        await db.execute(
            text("INSERT INTO ticket_events (ticket_id, tenant_id, from_status, to_status, actor, reason) "
                 "VALUES (:tid, :tenant, NULL, 'NEW', :actor, '提交工单')"),
            {"tid": row.id, "tenant": tenant_id, "actor": requester_name or "system"},
        )
    ticket = _row(row)
    logger.info("ticket.created", ticket_no=ticket_no, tenant_id=tenant_id)
    return ticket


async def get_ticket(ticket_id: str, tenant_id: str) -> Optional[dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            text(f"SELECT {_TICKET_COLUMNS} FROM tickets WHERE id = :id AND tenant_id = :tenant"),
            {"id": ticket_id, "tenant": tenant_id},
        )).fetchone()
    return _row(row) if row else None


async def require_ticket(ticket_id: str, tenant_id: str) -> dict[str, Any]:
    ticket = await get_ticket(ticket_id, tenant_id)
    if ticket is None:
        raise TicketNotFoundError(ticket_id)
    return ticket


async def get_username(user_id: Optional[str], tenant_id: str) -> Optional[str]:
    if not user_id:
        return None
    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            text("SELECT username FROM users WHERE id = :id AND tenant_id = :tenant"),
            {"id": user_id, "tenant": tenant_id},
        )).fetchone()
    return row.username if row else None


async def list_tickets(
    tenant_id: str,
    status: Optional[str] = None,
    requester_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = ["tenant_id = :tenant"], {"tenant": tenant_id, "limit": limit, "offset": offset}
    if status:
        where.append("status = :status")
        params["status"] = status
    if requester_id:
        where.append("requester_id = :rid")
        params["rid"] = requester_id
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text(f"SELECT {_TICKET_COLUMNS} FROM tickets WHERE {' AND '.join(where)} "
                 "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"),
            params,
        )).fetchall()
    return [_row(r) for r in rows]


async def update_fields(ticket_id: str, tenant_id: str, **fields: Any) -> dict[str, Any]:
    """只改业务字段、不改状态（如补充分类结果）。"""
    set_sql, params = _set_clause(fields)
    async with AsyncSessionLocal() as db, db.begin():
        row = (await db.execute(
            text(f"UPDATE tickets SET {set_sql}, updated_at = NOW() "
                 f"WHERE id = :id AND tenant_id = :tenant RETURNING {_TICKET_COLUMNS}"),
            {**params, "id": ticket_id, "tenant": tenant_id},
        )).fetchone()
    if row is None:
        raise TicketNotFoundError(ticket_id)
    return _row(row)


async def transition(
    ticket_id: str,
    tenant_id: str,
    to_status: TicketStatus,
    actor: str,
    reason: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """
    唯一的状态变更入口。SELECT ... FOR UPDATE 串行化同一工单的并发流转，
    白名单外的流转抛 InvalidTransitionError，事务回滚、状态不变。
    """
    to_status = TicketStatus(to_status)
    now = datetime.now(timezone.utc)
    if to_status == TicketStatus.RESOLVED:
        fields.setdefault("resolved_at", now)
    if to_status == TicketStatus.CLOSED:
        fields.setdefault("closed_at", now)

    async with AsyncSessionLocal() as db, db.begin():
        current = (await db.execute(
            text("SELECT status FROM tickets WHERE id = :id AND tenant_id = :tenant FOR UPDATE"),
            {"id": ticket_id, "tenant": tenant_id},
        )).fetchone()
        if current is None:
            raise TicketNotFoundError(ticket_id)
        from_status = TicketStatus(current.status)
        assert_transition(from_status, to_status)

        set_sql, params = _set_clause(fields) if fields else ("", {})
        row = (await db.execute(
            text(f"UPDATE tickets SET status = :to, version = version + 1, updated_at = NOW()"
                 f"{', ' + set_sql if set_sql else ''} "
                 f"WHERE id = :id AND tenant_id = :tenant RETURNING {_TICKET_COLUMNS}"),
            {**params, "to": to_status.value, "id": ticket_id, "tenant": tenant_id},
        )).fetchone()
        await db.execute(
            text("INSERT INTO ticket_events (ticket_id, tenant_id, from_status, to_status, actor, reason) "
                 "VALUES (:tid, :tenant, :from_s, :to_s, :actor, :reason)"),
            {"tid": ticket_id, "tenant": tenant_id, "from_s": from_status.value,
             "to_s": to_status.value, "actor": actor, "reason": reason},
        )
    logger.info("ticket.transition", ticket_id=ticket_id, from_status=from_status.value,
                to_status=to_status.value, actor=actor)
    return _row(row)


async def record_retrieval(ticket_id: str, tenant_id: str, query: str, doc_type: Optional[str],
                           result: dict[str, Any]) -> None:
    """记录一次检索，同时把本工单的检索置信度更新为历次最高值（代码测量值，自动结案判定用，不信任模型自评）。"""
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            text("INSERT INTO ticket_retrievals (ticket_id, tenant_id, query, doc_type, strategy, confidence, docs) "
                 "VALUES (:tid, :tenant, :q, :dt, :st, :c, CAST(:docs AS JSONB))"),
            {"tid": ticket_id, "tenant": tenant_id, "q": query, "dt": doc_type,
             "st": result.get("strategy"), "c": result["confidence"],
             "docs": json.dumps(result["docs"], ensure_ascii=False)},
        )
        await db.execute(
            text("UPDATE tickets SET retrieval_confidence = GREATEST(COALESCE(retrieval_confidence, 0), :c) "
                 "WHERE id = :id AND tenant_id = :tenant"),
            {"c": result["confidence"], "id": ticket_id, "tenant": tenant_id},
        )


async def list_retrievals(ticket_id: str, tenant_id: str) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text("SELECT query, doc_type, strategy, confidence, docs, created_at FROM ticket_retrievals "
                 "WHERE ticket_id = :id AND tenant_id = :tenant ORDER BY id"),
            {"id": ticket_id, "tenant": tenant_id},
        )).fetchall()
    return [_row(r) for r in rows]


async def list_events(ticket_id: str, tenant_id: str) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text("SELECT id, ticket_id, from_status, to_status, actor, reason, created_at "
                 "FROM ticket_events WHERE ticket_id = :id AND tenant_id = :tenant ORDER BY id"),
            {"id": ticket_id, "tenant": tenant_id},
        )).fetchall()
    return [_row(r) for r in rows]


async def find_sla_breached(now: datetime, limit: int = 100) -> list[dict[str, Any]]:
    """系统级扫描（跨租户）：只返回 SLA 计时中且已超时的工单。"""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text("SELECT id, tenant_id, ticket_no, status, priority, sla_due_at FROM tickets "
                 "WHERE status IN ('NEW', 'TRIAGED', 'IN_PROGRESS', 'PENDING_APPROVAL') "
                 "AND sla_due_at IS NOT NULL AND sla_due_at <= :now "
                 "ORDER BY sla_due_at LIMIT :limit"),
            {"now": now, "limit": limit},
        )).fetchall()
    return [_row(r) for r in rows]


# ── 审批 ────────────────────────────────────────────────────

async def create_approvals(
    ticket_id: str, tenant_id: str, interrupt_id: str, action_requests: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """为一次 HITL 中断建审批单；同一 interrupt_id 重复调用是幂等的。"""
    created = []
    async with AsyncSessionLocal() as db, db.begin():
        exists = (await db.execute(
            text("SELECT 1 FROM ticket_approvals WHERE ticket_id = :tid AND interrupt_id = :iid LIMIT 1"),
            {"tid": ticket_id, "iid": interrupt_id},
        )).fetchone()
        if exists:
            return []
        for seq, req in enumerate(action_requests):
            row = (await db.execute(
                text("INSERT INTO ticket_approvals "
                     "(ticket_id, tenant_id, interrupt_id, seq, tool_name, tool_args, risk_note) "
                     "VALUES (:tid, :tenant, :iid, :seq, :tool, CAST(:args AS JSONB), :note) "
                     "RETURNING id, ticket_id, interrupt_id, seq, tool_name, tool_args, risk_note, "
                     "status, requested_at"),
                {"tid": ticket_id, "tenant": tenant_id, "iid": interrupt_id, "seq": seq,
                 "tool": req["name"], "args": json.dumps(req.get("args", {}), ensure_ascii=False),
                 "note": req.get("description")},
            )).fetchone()
            created.append(_row(row))
    return created


async def list_approvals(
    tenant_id: str, status: Optional[str] = None, ticket_id: Optional[str] = None
) -> list[dict[str, Any]]:
    where, params = ["a.tenant_id = :tenant"], {"tenant": tenant_id}
    if status:
        where.append("a.status = :status")
        params["status"] = status
    if ticket_id:
        where.append("a.ticket_id = :tid")
        params["tid"] = ticket_id
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text("SELECT a.id, a.ticket_id, a.interrupt_id, a.seq, a.tool_name, a.tool_args, a.risk_note, "
                 "a.status, a.final_args, a.reviewer_id, a.reviewer_name, a.comment, a.requested_at, "
                 "a.decided_at, t.ticket_no, t.title, t.priority, t.status AS ticket_status "
                 "FROM ticket_approvals a JOIN tickets t ON t.id = a.ticket_id "
                 f"WHERE {' AND '.join(where)} ORDER BY a.requested_at, a.seq"),
            params,
        )).fetchall()
    return [_row(r) for r in rows]


async def decide_approval(
    approval_id: str,
    tenant_id: str,
    status: str,
    reviewer_id: str,
    reviewer_name: str,
    final_args: Optional[dict] = None,
    comment: Optional[str] = None,
) -> None:
    async with AsyncSessionLocal() as db, db.begin():
        result = await db.execute(
            text("UPDATE ticket_approvals SET status = :status, final_args = CAST(:final AS JSONB), "
                 "reviewer_id = :rid, reviewer_name = :rname, comment = :comment, decided_at = NOW() "
                 "WHERE id = :id AND tenant_id = :tenant AND status = 'pending'"),
            {"status": status, "final": json.dumps(final_args, ensure_ascii=False) if final_args else None,
             "rid": reviewer_id, "rname": reviewer_name, "comment": comment,
             "id": approval_id, "tenant": tenant_id},
        )
        if result.rowcount != 1:
            raise ValueError(f"审批单 {approval_id} 不存在或已处理")


# ── 运维操作日志 / 运行埋点 ─────────────────────────────────

async def record_ops_action(ticket_id: str, tenant_id: str, tool_name: str, args: dict, result: str) -> None:
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            text("INSERT INTO ops_actions (ticket_id, tenant_id, tool_name, args, result) "
                 "VALUES (:tid, :tenant, :tool, CAST(:args AS JSONB), :result)"),
            {"tid": ticket_id, "tenant": tenant_id, "tool": tool_name,
             "args": json.dumps(args, ensure_ascii=False), "result": result},
        )


async def list_ops_actions(ticket_id: str, tenant_id: str) -> list[dict[str, Any]]:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            text("SELECT tool_name, args, result, executed_at FROM ops_actions "
                 "WHERE ticket_id = :id AND tenant_id = :tenant ORDER BY id"),
            {"id": ticket_id, "tenant": tenant_id},
        )).fetchall()
    return [_row(r) for r in rows]


async def stats(tenant_id: str, since: Optional[datetime] = None) -> dict[str, Any]:
    """运营指标，全部从落库数据计算：工单表、流转事件、审批单、运行埋点。"""
    params = {"tenant": tenant_id, "since": since or datetime(1970, 1, 1, tzinfo=timezone.utc)}
    async with AsyncSessionLocal() as db:
        by_status = {r.status: r.n for r in (await db.execute(
            text("SELECT status, COUNT(*) AS n FROM tickets WHERE tenant_id = :tenant "
                 "AND created_at >= :since GROUP BY status"), params)).fetchall()}
        t = (await db.execute(text("""
            SELECT
              COUNT(*) FILTER (WHERE status IN ('RESOLVED', 'CLOSED'))                         AS finished,
              COUNT(*) FILTER (WHERE status IN ('RESOLVED', 'CLOSED') AND auto_resolved)       AS auto_resolved,
              COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM ticket_events e WHERE e.ticket_id = tickets.id
                                             AND e.to_status = 'ESCALATED'))                   AS escalated,
              COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM ticket_approvals a
                                             WHERE a.ticket_id = tickets.id))                  AS with_approval,
              COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM ticket_runs r WHERE r.ticket_id = tickets.id)) AS processed
            FROM tickets WHERE tenant_id = :tenant AND created_at >= :since
        """), params)).fetchone()
        r = (await db.execute(text("""
            SELECT COUNT(*) AS runs,
                   COUNT(*) FILTER (WHERE fallback_level > 0) AS fallback_runs,
                   percentile_cont(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
            FROM ticket_runs WHERE tenant_id = :tenant AND finished_at IS NOT NULL AND started_at >= :since
        """), params)).fetchone()

    def rate(a, b):
        return round(a / b, 4) if b else None

    return {
        "by_status": by_status,
        "processed": t.processed,
        "finished": t.finished,
        "auto_resolve_rate": rate(t.auto_resolved, t.processed),
        "escalation_rate": rate(t.escalated, t.processed),
        "approval_rate": rate(t.with_approval, t.processed),
        "runs": r.runs,
        "fallback_rate": rate(r.fallback_runs, r.runs),
        "latency_p50_ms": round(r.p50) if r.p50 is not None else None,
        "latency_p95_ms": round(r.p95) if r.p95 is not None else None,
    }


async def start_run(ticket_id: str, tenant_id: str) -> int:
    async with AsyncSessionLocal() as db, db.begin():
        return (await db.execute(
            text("INSERT INTO ticket_runs (ticket_id, tenant_id) VALUES (:tid, :tenant) RETURNING id"),
            {"tid": ticket_id, "tenant": tenant_id},
        )).scalar_one()


async def finish_run(
    run_id: int,
    outcome: str,
    fallback_level: int = 0,
    retrieval_confidence: Optional[float] = None,
    error: Optional[str] = None,
) -> None:
    # latency_ms 只算系统处理耗时，扣除等待人工审批的时间
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            text("UPDATE ticket_runs r SET finished_at = NOW(), "
                 "latency_ms = CAST(EXTRACT(EPOCH FROM (NOW() - r.started_at - COALESCE(("
                 "  SELECT SUM(a.decided_at - a.requested_at) FROM ticket_approvals a "
                 "  WHERE a.ticket_id = r.ticket_id AND a.requested_at >= r.started_at "
                 "  AND a.decided_at IS NOT NULL), INTERVAL '0'))) * 1000 AS INT), "
                 "approvals = (SELECT COUNT(*) FROM ticket_approvals a "
                 "  WHERE a.ticket_id = r.ticket_id AND a.requested_at >= r.started_at), "
                 "outcome = :outcome, fallback_level = :fb, "
                 "retrieval_confidence = :conf, error = :err WHERE r.id = :id"),
            {"id": run_id, "outcome": outcome, "fb": fallback_level,
             "conf": retrieval_confidence, "err": error},
        )
