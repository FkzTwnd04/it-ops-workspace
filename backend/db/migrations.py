# backend/db/migrations.py
#
# 启动时自动执行的 Schema 补丁（全部幂等，可重复运行）。
# 规则：只写 IF NOT EXISTS 类 DDL；约束替换用 DROP CONSTRAINT IF EXISTS + ADD 成对出现。

from sqlalchemy import text

from backend.core.logger import get_logger
from backend.dependencies import AsyncSessionLocal

logger = get_logger(__name__)

_MIGRATIONS: list[tuple[str, str]] = [
    ("extension.uuid-ossp", 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'),

    # ── 用户角色：requester（提单人）/ ops（运维，可审批）/ admin ──
    ("users.display_name", "ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name VARCHAR(64)"),
    ("users.role_check.drop", "ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check"),
    ("users.role_check.add",
     "ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('requester', 'ops', 'admin'))"),

    # ── 工单主表 ──
    ("seq.ticket_no", "CREATE SEQUENCE IF NOT EXISTS ticket_no_seq"),
    ("table.tickets", """
        CREATE TABLE IF NOT EXISTS tickets (
            id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            ticket_no         VARCHAR(32) NOT NULL UNIQUE,
            tenant_id         VARCHAR(64) NOT NULL,
            title             VARCHAR(200) NOT NULL,
            description       TEXT NOT NULL,
            requester_id      UUID,
            requester_name    VARCHAR(64),
            host              VARCHAR(128),
            category          VARCHAR(32),
            priority          VARCHAR(4) NOT NULL DEFAULT 'P3',
            status            VARCHAR(24) NOT NULL DEFAULT 'NEW',
            sla_due_at        TIMESTAMPTZ,
            escalation_reason TEXT,
            triage            JSONB,
            resolution        JSONB,
            auto_resolved     BOOLEAN NOT NULL DEFAULT FALSE,
            fallback_used     BOOLEAN NOT NULL DEFAULT FALSE,
            version           INT NOT NULL DEFAULT 0,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            resolved_at       TIMESTAMPTZ,
            closed_at         TIMESTAMPTZ
        )"""),
    ("tickets.retrieval_confidence",
     "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS retrieval_confidence FLOAT"),
    ("tickets.reopen_count",
     "ALTER TABLE tickets ADD COLUMN IF NOT EXISTS reopen_count INT NOT NULL DEFAULT 0"),
    ("idx.tickets_tenant_status",
     "CREATE INDEX IF NOT EXISTS idx_tickets_tenant_status ON tickets (tenant_id, status, created_at DESC)"),
    ("idx.tickets_sla",
     "CREATE INDEX IF NOT EXISTS idx_tickets_sla ON tickets (sla_due_at) WHERE sla_due_at IS NOT NULL"),

    # ── 状态流转审计：每次流转一条，谁、何时、为什么 ──
    ("table.ticket_events", """
        CREATE TABLE IF NOT EXISTS ticket_events (
            id          BIGSERIAL PRIMARY KEY,
            ticket_id   UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            tenant_id   VARCHAR(64) NOT NULL,
            from_status VARCHAR(24),
            to_status   VARCHAR(24) NOT NULL,
            actor       VARCHAR(64) NOT NULL,
            reason      TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""),
    ("idx.ticket_events_ticket",
     "CREATE INDEX IF NOT EXISTS idx_ticket_events_ticket ON ticket_events (ticket_id, id)"),

    # ── 高危操作审批记录（审计用，永不删除）──
    ("table.ticket_approvals", """
        CREATE TABLE IF NOT EXISTS ticket_approvals (
            id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            ticket_id    UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            tenant_id    VARCHAR(64) NOT NULL,
            tool_name    VARCHAR(64) NOT NULL,
            tool_args    JSONB NOT NULL,
            risk_note    TEXT,
            status       VARCHAR(16) NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending', 'approved', 'edited', 'rejected')),
            final_args   JSONB,
            reviewer_id  UUID,
            reviewer_name VARCHAR(64),
            comment      TEXT,
            requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            decided_at   TIMESTAMPTZ
        )"""),
    # 一次 HITL 中断可能包含多个工具调用，resume 时 decisions 必须按原顺序对应
    ("ticket_approvals.interrupt_id",
     "ALTER TABLE ticket_approvals ADD COLUMN IF NOT EXISTS interrupt_id VARCHAR(64)"),
    ("ticket_approvals.seq",
     "ALTER TABLE ticket_approvals ADD COLUMN IF NOT EXISTS seq INT NOT NULL DEFAULT 0"),
    ("idx.ticket_approvals_pending",
     "CREATE INDEX IF NOT EXISTS idx_ticket_approvals_pending ON ticket_approvals (tenant_id, status, requested_at)"),

    # ── 检索记录：Agent 每次查知识库的 query、命中文档和置信度（审计与复盘用）──
    ("table.ticket_retrievals", """
        CREATE TABLE IF NOT EXISTS ticket_retrievals (
            id          BIGSERIAL PRIMARY KEY,
            ticket_id   UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            tenant_id   VARCHAR(64) NOT NULL,
            query       TEXT NOT NULL,
            doc_type    VARCHAR(16),
            strategy    VARCHAR(16),
            confidence  FLOAT NOT NULL,
            docs        JSONB NOT NULL,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""),

    # ── 运维操作执行日志（模拟执行器写入）──
    ("table.ops_actions", """
        CREATE TABLE IF NOT EXISTS ops_actions (
            id          BIGSERIAL PRIMARY KEY,
            ticket_id   UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            tenant_id   VARCHAR(64) NOT NULL,
            tool_name   VARCHAR(64) NOT NULL,
            args        JSONB NOT NULL,
            result      TEXT NOT NULL,
            executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )"""),

    # ── 运行埋点：每次 Agent 处理一条，评测脚本据此统计自动解决率 / 延迟 / 降级率 ──
    ("table.ticket_runs", """
        CREATE TABLE IF NOT EXISTS ticket_runs (
            id                   BIGSERIAL PRIMARY KEY,
            ticket_id            UUID NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
            tenant_id            VARCHAR(64) NOT NULL,
            started_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            finished_at          TIMESTAMPTZ,
            latency_ms           INT,
            outcome              VARCHAR(24),
            fallback_level       SMALLINT NOT NULL DEFAULT 0,
            approvals            INT NOT NULL DEFAULT 0,
            retrieval_confidence FLOAT,
            error                TEXT
        )"""),
    ("idx.ticket_runs_ticket",
     "CREATE INDEX IF NOT EXISTS idx_ticket_runs_ticket ON ticket_runs (ticket_id, id DESC)"),
]


async def run_migrations() -> None:
    """应用启动时执行所有 Schema 补丁；单条失败只告警，不阻断启动。"""
    async with AsyncSessionLocal() as session:
        for desc, sql in _MIGRATIONS:
            try:
                await session.execute(text(sql))
                await session.commit()
            except Exception as e:
                await session.rollback()
                if "already exists" not in str(e):
                    logger.warning("db.migration_failed", migration=desc, error=str(e))
    logger.info("db.migrations_done", count=len(_MIGRATIONS))
