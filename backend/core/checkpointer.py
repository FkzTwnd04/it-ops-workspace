# backend/core/checkpointer.py
# LangGraph Checkpoint 持久化到 PostgreSQL：工单流程在任意节点暂停（等审批 / 等用户确认）后，
# 服务重启也能从断点恢复。MemorySaver 只在进程内存里，重启即丢。

from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from backend.config import get_settings
from backend.core.logger import get_logger

logger = get_logger(__name__)

_pool: Optional[AsyncConnectionPool] = None
_saver: Optional[BaseCheckpointSaver] = None


async def init_checkpointer() -> BaseCheckpointSaver:
    """应用启动时调用一次：建连接池 + 建 checkpoint 表（幂等）。"""
    global _pool, _saver
    if _saver is not None:
        return _saver
    _pool = AsyncConnectionPool(
        conninfo=get_settings().checkpoint_conninfo,
        min_size=1,
        max_size=10,
        open=False,
        # AsyncPostgresSaver 要求：autocommit + 关闭预编译（兼容 pgbouncer）+ dict 行
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await _pool.open(wait=True, timeout=15)
    saver = AsyncPostgresSaver(_pool)
    await saver.setup()
    _saver = saver
    logger.info("checkpointer.postgres_ready")
    return _saver


def get_checkpointer() -> BaseCheckpointSaver:
    if _saver is None:
        raise RuntimeError("Checkpointer 尚未初始化：请先在应用启动时调用 init_checkpointer()")
    return _saver


def set_checkpointer(saver: Optional[BaseCheckpointSaver]) -> None:
    """测试用：注入 MemorySaver。"""
    global _saver
    _saver = saver


async def close_checkpointer() -> None:
    global _pool, _saver
    if _pool is not None:
        await _pool.close()
    _pool, _saver = None, None
