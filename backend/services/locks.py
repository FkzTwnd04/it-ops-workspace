# backend/services/locks.py
# 分布式锁：同一工单同一时刻只允许一次 Agent 运行；SLA 巡检多进程部署时只跑一份。
# Redis 不可用时降级为进程内 asyncio 锁（单进程仍然正确，多进程失去互斥，记告警）。

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

# 只有持有者才能释放：比较 token 后再删除，避免误删别人续上的锁
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""

_redis = None
_redis_unavailable = False
_local_locks: dict[str, asyncio.Lock] = {}


class LockBusyError(Exception):
    """锁被占用（例如同一工单已有一次 Agent 运行在进行中）。"""


async def _get_redis():
    global _redis, _redis_unavailable
    if _redis_unavailable:
        return None
    if _redis is None:
        import redis.asyncio as aioredis
        client = aioredis.from_url(get_settings().redis_url, socket_connect_timeout=2)
        try:
            await client.ping()
        except Exception as e:
            logger.warning("lock.redis_unavailable_fallback_local", error=str(e))
            _redis_unavailable = True
            return None
        _redis = client
    return _redis


@asynccontextmanager
async def distributed_lock(key: str, ttl_seconds: int = 600) -> AsyncIterator[None]:
    """非阻塞获取锁；拿不到立即抛 LockBusyError，由调用方决定提示用户还是跳过。"""
    client = await _get_redis()
    if client is None:
        lock = _local_locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            raise LockBusyError(key)
        async with lock:
            yield
        return

    token = uuid.uuid4().hex
    acquired = await client.set(f"lock:{key}", token, nx=True, ex=ttl_seconds)
    if not acquired:
        raise LockBusyError(key)
    try:
        yield
    finally:
        try:
            await client.eval(_RELEASE_SCRIPT, 1, f"lock:{key}", token)
        except Exception as e:
            logger.warning("lock.release_failed", key=key, error=str(e))


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


def reset_for_tests(unavailable: Optional[bool] = None) -> None:
    global _redis, _redis_unavailable
    _redis = None
    _local_locks.clear()
    if unavailable is not None:
        _redis_unavailable = unavailable
