"""L2: distributed cache, backed by Redis.

Unlike L1 (per-process), this tier is shared across every query engine
node, which is what makes the caching strategy "distributed": a result
computed and cached by node A is immediately visible to nodes B and C.

Entries are stored as a single msgpack-free pickle+zlib blob containing both
the result payload and the table_versions map it was computed from, so a
version check (see invalidation.py) never requires a second round trip.
"""
from __future__ import annotations

import pickle
import time
import zlib
from dataclasses import dataclass
from typing import Any


@dataclass
class RedisEntry:
    value: Any
    table_versions: dict[str, int]
    created_at: float


class RedisCache:
    KEY_PREFIX = "cacheopt:qr:"

    def __init__(self, client, default_ttl_seconds: int = 300):
        self._r = client
        self._default_ttl = default_ttl_seconds
        self.hits = 0
        self.misses = 0

    def _k(self, cache_key: str) -> str:
        return f"{self.KEY_PREFIX}{cache_key}"

    def get(self, cache_key: str) -> RedisEntry | None:
        blob = self._r.get(self._k(cache_key))
        if blob is None:
            self.misses += 1
            return None
        try:
            entry = pickle.loads(zlib.decompress(blob))
        except Exception:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, cache_key: str, value: Any, table_versions: dict[str, int], ttl_seconds: float | None = None):
        entry = RedisEntry(value=value, table_versions=table_versions, created_at=time.time())
        blob = zlib.compress(pickle.dumps(entry), level=1)
        ttl = int(ttl_seconds if ttl_seconds is not None else self._default_ttl)
        self._r.set(self._k(cache_key), blob, ex=ttl)

    def invalidate(self, cache_key: str):
        self._r.delete(self._k(cache_key))

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses, "hit_rate": self.hits / total if total else 0.0}
