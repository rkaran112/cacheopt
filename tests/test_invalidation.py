import time

from cacheopt.cache.memory_buffer import MemoryBuffer
from cacheopt.cache.redis_cache import RedisCache
from cacheopt.cache.redis_client import get_redis_client
from cacheopt.cache.tiered_cache import TieredCache, TierHit
from cacheopt.invalidation import InvalidationSubscriber, TableVersionManager


def test_version_bump_is_monotonic(config):
    client = get_redis_client(config)
    vm = TableVersionManager(client)
    v1 = vm.bump_and_publish("orders")
    v2 = vm.bump_and_publish("orders")
    assert v2 == v1 + 1


def test_l2_read_detects_staleness_via_version_check(config):
    client = get_redis_client(config)
    vm = TableVersionManager(client)
    cache = TieredCache(MemoryBuffer(), RedisCache(client), vm)

    cache.put("q1", ("cols", [(1,)]), tables=("orders",), write_l1=False, write_l2=True)
    # check_l1=False isolates L2 behavior: a normal get() would opportunistically
    # promote an L2 hit into L1, and L1 is (by design, see invalidation.py)
    # only kept fresh via pub/sub rather than a version check on every read.
    hit = cache.get("q1", tables=("orders",), check_l1=False)
    assert hit.hit == TierHit.L2

    # simulate a write to "orders" bumping its version after the cache entry
    # was created -- the *next* read must treat the entry as stale, giving
    # strong consistency without waiting on TTL expiry.
    vm.bump_and_publish("orders")
    hit2 = cache.get("q1", tables=("orders",), check_l1=False)
    assert hit2.hit == TierHit.MISS


def test_pubsub_invalidation_purges_l1_across_nodes(config):
    client = get_redis_client(config)
    vm = TableVersionManager(client)

    node_a_memory = MemoryBuffer()
    node_b_memory = MemoryBuffer()

    node_a_memory.put("q1", "result-a", table_versions={"orders": 0})
    node_b_memory.put("q1", "result-b", table_versions={"orders": 0})

    sub_a = InvalidationSubscriber(client, lambda tables, versions: node_a_memory.invalidate_tables(tables, versions))
    sub_b = InvalidationSubscriber(client, lambda tables, versions: node_b_memory.invalidate_tables(tables, versions))
    sub_a.start()
    sub_b.start()
    try:
        time.sleep(0.1)  # let subscriptions register
        vm.bump_and_publish("orders")
        time.sleep(0.3)  # allow pub/sub delivery

        assert node_a_memory.get("q1") is None
        assert node_b_memory.get("q1") is None
    finally:
        sub_a.stop()
        sub_b.stop()
