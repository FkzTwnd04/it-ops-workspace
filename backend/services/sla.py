# backend/services/sla.py
# SLA 巡检：定时扫描计时中且已超时的工单，自动升级为 ESCALATED。
# 多实例部署时用分布式锁保证同一时刻只有一个实例在扫；
# 与 Agent 运行的并发由 transition 的行锁 + 白名单兜住（工单已被推进到别的状态时跳过）。

import asyncio
from datetime import datetime, timezone
from typing import Optional

from backend.agents.ticket.states import InvalidTransitionError, TicketStatus
from backend.config import get_settings
from backend.core.logger import get_logger
from backend.services import ticket_repo
from backend.services.locks import LockBusyError, distributed_lock

logger = get_logger(__name__)


async def scan_once(now: Optional[datetime] = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    escalated: list[str] = []
    try:
        async with distributed_lock("sla:scanner", ttl_seconds=get_settings().sla_scan_interval_seconds):
            for t in await ticket_repo.find_sla_breached(now):
                reason = f"SLA 超时：{t['priority']} 应在 {t['sla_due_at']:%m-%d %H:%M} 前处理（当前 {t['status']}）"
                try:
                    await ticket_repo.transition(str(t["id"]), t["tenant_id"], TicketStatus.ESCALATED,
                                                 "sla_monitor", reason, escalation_reason=reason)
                    escalated.append(t["ticket_no"])
                except InvalidTransitionError:
                    continue
    except LockBusyError:
        return []
    if escalated:
        logger.warning("sla.escalated", count=len(escalated), tickets=escalated)
    return escalated


async def sla_loop(stop: asyncio.Event) -> None:
    interval = get_settings().sla_scan_interval_seconds
    while not stop.is_set():
        try:
            await scan_once()
        except Exception as e:
            logger.error("sla.scan_failed", error=str(e))
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
