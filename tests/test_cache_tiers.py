import time

from cacheopt.cache.memory_buffer import MemoryBuffer


def test_l1_basic_get_put():
    mb = MemoryBuffer(max_entries=10, max_bytes=1_000_000)
    assert mb.get("k1") is None
    mb.put("k1", "value1", table_versions={"t": 1})
    entry = mb.get("k1")
    assert entry is not None
    assert entry.value == "value1"


def test_l1_lru_eviction_by_entry_count():
    mb = MemoryBuffer(max_entries=2, max_bytes=1_000_000)
    mb.put("a", 1, {})
    mb.put("b", 2, {})
    mb.put("c", 3, {})  # evicts "a" (least recently used)
    assert mb.get("a") is None
    assert mb.get("b") is not None
    assert mb.get("c") is not None


def test_l1_ttl_expiry():
    mb = MemoryBuffer()
    mb.put("k", "v", {}, ttl_seconds=0.05)
    assert mb.get("k") is not None
    time.sleep(0.1)
    assert mb.get("k") is None


def test_l1_invalidate_tables_drops_stale_entries_only():
    mb = MemoryBuffer()
    mb.put("stale", "v1", {"orders": 1})
    mb.put("fresh", "v2", {"orders": 2})
    mb.invalidate_tables({"orders"}, {"orders": 2})
    assert mb.get("stale") is None
    assert mb.get("fresh") is not None


def test_tiered_cache_shares_l2_across_nodes(config):
    from cacheopt.cache.memory_buffer import MemoryBuffer
    from cacheopt.cache.redis_cache import RedisCache
    from cacheopt.cache.redis_client import get_redis_client
    from cacheopt.cache.tiered_cache import TieredCache, TierHit
    from cacheopt.invalidation import TableVersionManager

    client = get_redis_client(config)
    version_mgr = TableVersionManager(client)

    node_a = TieredCache(MemoryBuffer(), RedisCache(client), version_mgr)
    node_b = TieredCache(MemoryBuffer(), RedisCache(client), version_mgr)

    node_a.put("key1", ("cols", [(1, 2)]), tables=("orders",), write_l1=True, write_l2=True)

    # node_b has never seen this key locally (separate L1), but shares Redis
    result = node_b.get("key1", tables=("orders",))
    assert result.hit == TierHit.L2
    assert result.value == ("cols", [(1, 2)])
