"""三层兜底：重试 → 降级函数 → 系统兜底；interrupt 信号不能被当成错误吞掉。"""
import asyncio

import pytest
from langgraph.errors import GraphInterrupt

from backend.core import retry
from backend.core.exceptions import InvalidInputError, LLMAPIError


@pytest.fixture(autouse=True)
def fast_retry(monkeypatch):
    monkeypatch.setattr(retry, "RETRY_DELAYS", [0.0, 0.0])


async def test_retry_then_success():
    calls = []

    @retry.with_retry("t_ok")
    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise LLMAPIError("boom")
        return "ok"

    assert await flaky() == "ok"
    assert len(calls) == 3


async def test_fallback_after_retries_exhausted():
    calls = []

    @retry.register_fallback("t_fb")
    async def _fb(x, *, error=None):
        return {"fallback": x, "error": type(error).__name__}

    @retry.with_retry("t_fb")
    async def always_fail(x):
        calls.append(1)
        raise ConnectionError("down")

    assert await always_fail("q") == {"fallback": "q", "error": "ConnectionError"}
    assert len(calls) == 1 + retry.MAX_RETRIES


async def test_system_fallback_when_fallback_fails():
    @retry.register_fallback("t_sys")
    async def _fb(*, error=None):
        raise RuntimeError("fallback down")

    @retry.with_retry("t_sys")
    async def always_fail():
        raise TimeoutError()

    result = await always_fail()
    assert result["system_fallback"] is True and result["fallback_level"] == 3


async def test_timeout_is_retried():
    calls = []

    @retry.with_retry("t_timeout", timeout=0.01, max_retries=1)
    async def slow():
        calls.append(1)
        await asyncio.sleep(1)

    result = await slow()
    assert len(calls) == 2 and result["system_fallback"]


async def test_non_retryable_error_propagates_immediately():
    calls = []

    @retry.with_retry("t_invalid")
    async def bad():
        calls.append(1)
        raise InvalidInputError("bad input")

    with pytest.raises(InvalidInputError):
        await bad()
    assert len(calls) == 1


async def test_graph_interrupt_is_not_swallowed():
    """等待审批的 interrupt 是控制流，必须原样上抛，否则审批永远不会发生。"""
    calls = []

    @retry.with_retry("t_interrupt")
    async def needs_approval():
        calls.append(1)
        raise GraphInterrupt(())

    with pytest.raises(GraphInterrupt):
        await needs_approval()
    assert len(calls) == 1


def test_system_fallback_messages():
    assert "转人工" in retry.system_fallback("retrieval", None)["content"]
    assert "升级" in retry.system_fallback("ticket_agent", ValueError("x"))["content"]
    assert retry.system_fallback("unknown", None)["content"]


async def test_retrieval_public_api_always_returns_same_shape(monkeypatch):
    """检索链路彻底不可用时，调用方拿到的仍是标准结构（空结果 + 低置信度），不会 KeyError。"""
    from backend.agents.ticket import retrieval

    async def broken(*a, **k):
        return retry.system_fallback("retrieval", RuntimeError("x"))

    monkeypatch.setattr(retrieval, "_search_knowledge", broken)
    r = await retrieval.search_knowledge("VPN", "t1")
    assert r["docs"] == [] and r["low_confidence"] and r["fallback_used"] and r["confidence"] == 0.0
