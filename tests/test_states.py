"""工单状态机：8 个状态、流转白名单、SLA 策略。"""
from datetime import datetime, timedelta, timezone
from itertools import product

import pytest

from backend.agents.ticket.states import (
    SLA_POLICY,
    TRANSITIONS,
    InvalidTransitionError,
    Priority,
    TicketStatus,
    assert_transition,
    can_transition,
    compute_sla_due,
    is_sla_breached,
)

S = TicketStatus
ALLOWED = {
    (S.NEW, S.TRIAGED), (S.NEW, S.ESCALATED),
    (S.TRIAGED, S.IN_PROGRESS), (S.TRIAGED, S.ESCALATED),
    (S.IN_PROGRESS, S.PENDING_APPROVAL), (S.IN_PROGRESS, S.PENDING_CONFIRM), (S.IN_PROGRESS, S.ESCALATED),
    (S.PENDING_APPROVAL, S.IN_PROGRESS), (S.PENDING_APPROVAL, S.ESCALATED),
    (S.PENDING_CONFIRM, S.RESOLVED), (S.PENDING_CONFIRM, S.IN_PROGRESS), (S.PENDING_CONFIRM, S.ESCALATED),
    (S.RESOLVED, S.CLOSED), (S.RESOLVED, S.IN_PROGRESS),
    (S.ESCALATED, S.IN_PROGRESS), (S.ESCALATED, S.RESOLVED),
}


def test_eight_states():
    assert len(TicketStatus) == 8
    assert set(TRANSITIONS) == set(TicketStatus)


@pytest.mark.parametrize("src,dst", list(product(TicketStatus, TicketStatus)),
                         ids=lambda s: s.value)
def test_transition_matrix(src, dst):
    """64 种组合逐一校验：只有白名单内的流转被允许。"""
    assert can_transition(src, dst) == ((src, dst) in ALLOWED)
    if (src, dst) not in ALLOWED:
        with pytest.raises(InvalidTransitionError):
            assert_transition(src, dst)


def test_closed_is_terminal():
    assert TRANSITIONS[S.CLOSED] == set()


def test_every_non_terminal_state_can_escalate_or_finish():
    for s in TicketStatus:
        if s in (S.CLOSED, S.ESCALATED, S.RESOLVED):
            continue
        assert can_transition(s, S.ESCALATED), s


@pytest.mark.parametrize("prio,delta", [("P1", timedelta(minutes=30)), ("P2", timedelta(hours=2)),
                                        ("P3", timedelta(hours=8)), ("P4", timedelta(hours=24))])
def test_sla_policy(prio, delta):
    start = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
    assert SLA_POLICY[Priority(prio)] == delta
    assert compute_sla_due(prio, start) == start + delta


def test_sla_breach_only_for_tracked_statuses():
    due = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
    late = due + timedelta(seconds=1)
    assert is_sla_breached(S.IN_PROGRESS, due, late)
    assert is_sla_breached(S.PENDING_APPROVAL, due, late)
    assert not is_sla_breached(S.IN_PROGRESS, due, due - timedelta(seconds=1))
    assert not is_sla_breached(S.PENDING_CONFIRM, due, late)   # 等用户确认不计 SLA
    assert not is_sla_breached(S.ESCALATED, due, late)
    assert not is_sla_breached(S.NEW, None, late)
