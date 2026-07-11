"""L1: per-node in-process memory buffer.

This is the fastest, cheapest tier (no serialization, no network hop) but is
local to a single query engine process/node and bounded in size. It uses a
size-aware LRU with an approximate byte budget, which is the standard
admission/eviction policy for in-process result caches (comparable in spirit
to Guava/Caffeine's local cache).
"""
from __future__ import annotations

import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


@dataclass
class CacheEntry:
    value: Any
    table_versions: dict[str, int]
    created_at: float
    expires_at: float | None
    size_bytes: int


class MemoryBuffer:
    def __init__(self, max_entries: int = 512, max_bytes: int = 64 * 1024 * 1024):
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._store: "OrderedDict[str, CacheEntry]" = OrderedDict()
        self._bytes_used = 0
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key: str) -> CacheEntry | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self.misses += 1
                return None
            if entry.expires_at is not None and entry.expires_at < time.time():
                self._evict(key)
                self.misses += 1
                return None
            self._store.move_to_end(key)
            self.hits += 1
            return entry

    def put(self, key: str, value: Any, table_versions: dict[str, int], ttl_seconds: float | None = None):
        size = _approx_size(value)
        expires_at = time.time() + ttl_seconds if ttl_seconds else None
        entry = CacheEntry(value=value, table_versions=table_versions, created_at=time.time(),
                            expires_at=expires_at, size_bytes=size)
        with self._lock:
            if key in self._store:
                self._bytes_used -= self._store[key].size_bytes
                del self._store[key]
            self._store[key] = entry
            self._bytes_used += size
            self._enforce_limits()

    def invalidate(self, key: str):
        with self._lock:
            self._evict(key)

    def invalidate_tables(self, tables: set[str], current_versions: dict[str, int]):
        """Drop every entry whose captured table_versions are stale relative
        to `current_versions`. Called synchronously from the pub/sub
        invalidation listener (see invalidation.py)."""
        with self._lock:
            stale = [
                k for k, e in self._store.items()
                if any(t in tables and e.table_versions.get(t, -1) < current_versions.get(t, 0) for t in e.table_versions)
            ]
            for k in stale:
                self._evict(k)

    def _evict(self, key: str):
        entry = self._store.pop(key, None)
        if entry is not None:
            self._bytes_used -= entry.size_bytes
            self.evictions += 1

    def _enforce_limits(self):
        while len(self._store) > self._max_entries or self._bytes_used > self._max_bytes:
            if not self._store:
                break
            oldest_key, oldest_entry = next(iter(self._store.items()))
            self._store.popitem(last=False)
            self._bytes_used -= oldest_entry.size_bytes
            self.evictions += 1

    def stats(self) -> dict:
        with self._lock:
            total = self.hits + self.misses
            return {
                "entries": len(self._store),
                "bytes_used": self._bytes_used,
                "hits": self.hits,
                "misses": self.misses,
                "hit_rate": self.hits / total if total else 0.0,
                "evictions": self.evictions,
            }


def _approx_size(value: Any) -> int:
    try:
        if isinstance(value, list):
            return sum(sys.getsizeof(row) + sum(sys.getsizeof(c) for c in row) for row in value[:50]) * max(1, len(value) // 50) + sys.getsizeof(value)
        return sys.getsizeof(value)
    except Exception:
        return 1024
