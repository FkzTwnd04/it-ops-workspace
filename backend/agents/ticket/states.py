# backend/agents/ticket/states.py
# 工单状态机规则：8 种状态 + 流转白名单 + SLA 时限。
# 纯规则、无 IO：状态怎么流转只由这里决定，大模型只提供数据（分类、方案、置信度），不决定流转。

from datetime import datetime, timedelta, timezone
from enum import Enum


class TicketStatus(str, Enum):
    NEW              = "NEW"               # 新建
    TRIAGED          = "TRIAGED"           # 已分类
    IN_PROGRESS      = "IN_PROGRESS"       # 处理中
    PENDING_APPROVAL = "PENDING_APPROVAL"  # 待审批（高危操作等待运维审批）
    PENDING_CONFIRM  = "PENDING_CONFIRM"   # 待用户确认
    RESOLVED         = "RESOLVED"          # 已解决
    CLOSED           = "CLOSED"            # 已关闭
    ESCALATED        = "ESCALATED"         # 已升级（转人工）


STATUS_LABELS: dict[TicketStatus, str] = {
    TicketStatus.NEW:              "新建",
    TicketStatus.TRIAGED:          "已分类",
    TicketStatus.IN_PROGRESS:      "处理中",
    TicketStatus.PENDING_APPROVAL: "待审批",
    TicketStatus.PENDING_CONFIRM:  "待用户确认",
    TicketStatus.RESOLVED:         "已解决",
    TicketStatus.CLOSED:           "已关闭",
    TicketStatus.ESCALATED:        "已升级",
}

S = TicketStatus
TRANSITIONS: dict[TicketStatus, frozenset[TicketStatus]] = {
    S.NEW:              frozenset({S.TRIAGED, S.ESCALATED}),
    S.TRIAGED:          frozenset({S.IN_PROGRESS, S.ESCALATED}),
    S.IN_PROGRESS:      frozenset({S.PENDING_APPROVAL, S.PENDING_CONFIRM, S.ESCALATED}),
    S.PENDING_APPROVAL: frozenset({S.IN_PROGRESS, S.ESCALATED}),
    S.PENDING_CONFIRM:  frozenset({S.RESOLVED, S.IN_PROGRESS, S.ESCALATED}),
    S.RESOLVED:         frozenset({S.CLOSED, S.IN_PROGRESS}),
    S.ESCALATED:        frozenset({S.IN_PROGRESS, S.RESOLVED}),
    S.CLOSED:           frozenset(),
}

TERMINAL_STATUSES = frozenset({S.CLOSED})

# 计入 SLA 的状态：等待用户确认、已解决、已升级（已在人工手里）不再计时
SLA_TRACKED_STATUSES = frozenset({S.NEW, S.TRIAGED, S.IN_PROGRESS, S.PENDING_APPROVAL})


class Priority(str, Enum):
    P1 = "P1"   # 核心业务中断
    P2 = "P2"   # 多人受影响
    P3 = "P3"   # 单人受影响，有临时方案
    P4 = "P4"   # 咨询 / 低影响


SLA_POLICY: dict[Priority, timedelta] = {
    Priority.P1: timedelta(minutes=30),
    Priority.P2: timedelta(hours=2),
    Priority.P3: timedelta(hours=8),
    Priority.P4: timedelta(hours=24),
}

CATEGORIES: dict[str, str] = {
    "account":    "账号与密码",
    "network":    "网络与 VPN",
    "hardware":   "硬件与外设",
    "software":   "软件安装与故障",
    "permission": "权限申请",
    "email":      "邮箱与协作",
    "other":      "其他",
}


class InvalidTransitionError(Exception):
    """非法流转：不在白名单内。"""

    def __init__(self, from_status: TicketStatus, to_status: TicketStatus):
        super().__init__(f"非法状态流转：{from_status.value} → {to_status.value}")
        self.from_status = from_status
        self.to_status = to_status


def can_transition(from_status: TicketStatus | str, to_status: TicketStatus | str) -> bool:
    return TicketStatus(to_status) in TRANSITIONS[TicketStatus(from_status)]


def assert_transition(from_status: TicketStatus | str, to_status: TicketStatus | str) -> None:
    if not can_transition(from_status, to_status):
        raise InvalidTransitionError(TicketStatus(from_status), TicketStatus(to_status))


def compute_sla_due(priority: Priority | str, start: datetime | None = None) -> datetime:
    start = start or datetime.now(timezone.utc)
    return start + SLA_POLICY[Priority(priority)]


def is_sla_breached(status: TicketStatus | str, sla_due_at: datetime | None, now: datetime | None = None) -> bool:
    if sla_due_at is None or TicketStatus(status) not in SLA_TRACKED_STATUSES:
        return False
    return (now or datetime.now(timezone.utc)) >= sla_due_at
