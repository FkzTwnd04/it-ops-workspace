"""分布式锁：同一工单同一时刻只允许一次运行；Redis 不可用时降级为进程内锁。"""
import uuid

import pytest

from backend.services import locks
from backend.services.locks import LockBusyError, distributed_lock


@pytest.fixture(params=["local", "redis"])
async def lock_backend(request):
    locks.reset_for_tests(unavailable=(request.param == "local"))
    if request.param == "redis" and await locks._get_redis() is None:
        pytest.skip("Redis 不可用")
    yield request.param
    await locks.close_redis()
    locks.reset_for_tests(unavailable=False)


async def test_second_holder_is_rejected(lock_backend):
    key = f"test:{uuid.uuid4()}"
    async with distributed_lock(key, ttl_seconds=5):
        with pytest.raises(LockBusyError):
            async with distributed_lock(key, ttl_seconds=5):
                pass


async def test_released_after_exit(lock_backend):
    key = f"test:{uuid.uuid4()}"
    async with distributed_lock(key, ttl_seconds=5):
        pass
    async with distributed_lock(key, ttl_seconds=5):
        pass


async def test_released_after_exception(lock_backend):
    key = f"test:{uuid.uuid4()}"
    with pytest.raises(RuntimeError):
        async with distributed_lock(key, ttl_seconds=5):
            raise RuntimeError("boom")
    async with distributed_lock(key, ttl_seconds=5):
        pass


async def test_different_keys_do_not_block(lock_backend):
    async with distributed_lock(f"test:{uuid.uuid4()}", ttl_seconds=5):
        async with distributed_lock(f"test:{uuid.uuid4()}", ttl_seconds=5):
            pass


async def test_redis_only_owner_can_release():
    """锁过期后被别人拿到，原持有者退出时不能把别人的锁删掉。"""
    locks.reset_for_tests(unavailable=False)
    client = await locks._get_redis()
    if client is None:
        pytest.skip("Redis 不可用")
    key = f"test:{uuid.uuid4()}"
    async with distributed_lock(key, ttl_seconds=5):
        await client.set(f"lock:{key}", "someone-else")
    assert await client.get(f"lock:{key}") == b"someone-else"
    await client.delete(f"lock:{key}")
    await locks.close_redis()
