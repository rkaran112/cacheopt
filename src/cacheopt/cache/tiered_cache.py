"""Unifies L1 (memory_buffer) and L2 (redis_cache) into a single lookup path
with version-checked strong consistency, plus the promotion/demotion logic
that makes the caching strategy "adaptive": hot query results get pushed
into the fast local L1 tier, cold ones stay in L2 (or aren't cached at all).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from ..invalidation import TableVersionManager
from .memory_buffer import MemoryBuffer
from .redis_cache import RedisCache


class TierHit(str, Enum):
    L1 = "L1_MEMORY"
    L2 = "L2_REDIS"
    MISS = "MISS"


@dataclass
class CacheLookupResult:
    hit: TierHit
    value: Any | None


class TieredCache:
    def __init__(self, memory: MemoryBuffer, redis_cache: RedisCache, version_mgr: TableVersionManager):
        self.memory = memory
        self.redis_cache = redis_cache
        self.version_mgr = version_mgr

    def get(self, cache_key: str, tables: tuple[str, ...], check_l1: bool = True, check_l2: bool = True) -> CacheLookupResult:
        if check_l1:
            entry = self.memory.get(cache_key)
            if entry is not None:
                # L1 is kept fresh via synchronous pub/sub invalidation
                # (see invalidation.py); no extra Redis round trip needed
                # on the hot path.
                return CacheLookupResult(hit=TierHit.L1, value=entry.value)

        if check_l2:
            entry = self.redis_cache.get(cache_key)
            if entry is not None:
                if tables and self.version_mgr.is_stale(entry.table_versions, tables):
                    self.redis_cache.invalidate(cache_key)
                else:
                    if check_l1:
                        # Promote on read: an L2 hit that got re-requested is
                        # itself a recency signal, so opportunistically warm L1.
                        self.memory.put(cache_key, entry.value, entry.table_versions)
                    return CacheLookupResult(hit=TierHit.L2, value=entry.value)

        return CacheLookupResult(hit=TierHit.MISS, value=None)

    def put(self, cache_key: str, value: Any, tables: tuple[str, ...], write_l1: bool, write_l2: bool,
            ttl_seconds: float | None = None):
        table_versions = self.version_mgr.current_versions(tables)
        if write_l1:
            self.memory.put(cache_key, value, table_versions, ttl_seconds=ttl_seconds)
        if write_l2:
            self.redis_cache.put(cache_key, value, table_versions, ttl_seconds=ttl_seconds)

    def stats(self) -> dict:
        return {"l1": self.memory.stats(), "l2": self.redis_cache.stats()}
