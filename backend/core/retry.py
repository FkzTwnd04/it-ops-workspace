# backend/core/retry.py
# 三层兜底：自动重试 → Agent 级降级 → 系统级兜底。
#
# 在工单系统里的落点：
#   第一层  模型调用失败由 Agent 内的 ModelRetryMiddleware 按 1s / 3s 重试；
#           检索等普通异步调用用本文件的 @with_retry 重试。
#   第二层  重试耗尽 → 调用注册的降级函数（如 Agent 失败 → 只做检索，把建议方案交给人工）。
#   第三层  降级也失败 → 返回系统兜底结果，调用方据此把工单升级，保证流程不中断。

import asyncio
from functools import wraps
from typing import Any, Awaitable, Callable, Optional

from langgraph.errors import GraphBubbleUp

from backend.core.exceptions import (
    AuthenticationError,
    InvalidInputError,
    LLMAPIError,
    MilvusConnectionError,
)
from backend.core.logger import get_logger

logger = get_logger(__name__)

RETRYABLE_ERRORS = (LLMAPIError, MilvusConnectionError, TimeoutError, ConnectionError)
# GraphBubbleUp 是 LangGraph 的 interrupt 信号（等待审批），不是错误，必须原样上抛
NON_RETRYABLE_ERRORS = (InvalidInputError, AuthenticationError, GraphBubbleUp)

MAX_RETRIES = 2
RETRY_DELAYS = [1.0, 3.0]
DEFAULT_TIMEOUT = 60.0

FallbackFn = Callable[..., Awaitable[Any]]
_FALLBACKS: dict[str, FallbackFn] = {}


def register_fallback(agent_type: str) -> Callable[[FallbackFn], FallbackFn]:
    """注册第二层降级函数。降级函数收到与原函数相同的参数，外加 error=原始异常。"""
    def decorator(fn: FallbackFn) -> FallbackFn:
        _FALLBACKS[agent_type] = fn
        return fn
    return decorator


def with_retry(agent_type: str, timeout: float = DEFAULT_TIMEOUT, max_retries: int = MAX_RETRIES):
    """
    三层兜底装饰器工厂。

        @with_retry(agent_type="retrieval", timeout=30)
        async def search(query, tenant_id): ...
    """
    def decorator(func: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            # ── 第一层：自动重试 ──
            last_error: Optional[Exception] = None
            for attempt in range(max_retries + 1):
                try:
                    result = await asyncio.wait_for(func(*args, **kwargs), timeout=timeout)
                    if attempt > 0:
                        logger.info("retry.succeeded", agent_type=agent_type, attempt=attempt + 1)
                    return result
                except NON_RETRYABLE_ERRORS:
                    raise
                except Exception as e:
                    last_error = e
                    if attempt < max_retries:
                        delay = RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)]
                        logger.warning("retry.attempt_failed", agent_type=agent_type,
                                       attempt=attempt + 1, delay=delay, error=repr(e))
                        await asyncio.sleep(delay)
                    else:
                        logger.error("retry.all_attempts_failed", agent_type=agent_type, error=repr(e))

            # ── 第二层：Agent 级降级 ──
            fallback = _FALLBACKS.get(agent_type)
            if fallback is not None:
                try:
                    result = await fallback(*args, error=last_error, **kwargs)
                    logger.info("retry.fallback_succeeded", agent_type=agent_type)
                    return result
                except GraphBubbleUp:
                    raise
                except Exception as fallback_error:
                    logger.error("retry.fallback_failed", agent_type=agent_type, error=repr(fallback_error))

            # ── 第三层：系统级兜底 ──
            logger.error("retry.system_fallback", agent_type=agent_type, original_error=repr(last_error))
            return system_fallback(agent_type, last_error)
        return wrapper
    return decorator


_SYSTEM_MESSAGES = {
    "retrieval":    "知识库暂时不可用，本工单已转人工处理。",
    "ticket_agent": "智能处理服务暂时不可用，工单已自动升级给运维人员，您无需重复提交。",
}


def system_fallback(agent_type: str, error: Optional[Exception]) -> dict[str, Any]:
    """第三层：永远不会失败的保底结果。"""
    return {
        "system_fallback": True,
        "fallback_level": 3,
        "content": _SYSTEM_MESSAGES.get(agent_type, "服务暂时不可用，请稍后再试。"),
        "error": repr(error) if error else None,
    }
