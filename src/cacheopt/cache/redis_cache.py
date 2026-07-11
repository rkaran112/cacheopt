"""L2: distributed cache, backed by Redis.

Unlike L1 (per-process), this tier is shared across every query engine
node, which is what makes the caching strategy "distributed": a result
computed and cached by node A is immediately visible to nodes B and C.

Entries are stored as a single JSON+zlib blob containing both the result
payload and the table_versions map it was computed from, so a version check
(see invalidation.py) never requires a second round trip.

JSON (not pickle) is used deliberately: L2 is a network-shared cache visible
to every node in the fleet, so deserializing whatever bytes happen to be
under a cacheopt:qr:* key must never be able to execute code -- pickle.loads
on attacker-controlled or corrupted bytes is arbitrary code execution.
"""
from __future__ import annotations

import datetime
import decimal
import json
import time
import zlib
from dataclasses import dataclass
from typing import Any


@dataclass
class RedisEntry:
    value: Any
    table_versions: dict[str, int]
    created_at: float


def _restore_value(value: Any) -> Any:
    # JSON has no tuple type, so the (columns, rows) shape every caller in
    # this codebase actually stores round-trips as [columns_list, rows_list].
    # Restore it to (columns: tuple, rows: list[tuple]) to match what was put
    # in. Anything that isn't that 2-element shape is returned as JSON gave
    # it to us -- this cache is typed `Any`, so we can't assume more.
    if isinstance(value, list) and len(value) == 2 and isinstance(value[1], list):
        columns, rows = value
        columns = tuple(columns) if isinstance(columns, list) else columns
        return columns, [tuple(r) if isinstance(r, list) else r for r in rows]
    return value


def _json_default(obj: Any) -> Any:
    # DuckDB result rows can carry types JSON has no native form for --
    # stringify them rather than letting json.dumps raise. Cache values are
    # display data, not a source of truth, so this lossy-but-safe conversion
    # is fine (unlike pickle, it can never execute code on the way back in).
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return str(obj)


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
            payload = json.loads(zlib.decompress(blob))
            entry = RedisEntry(
                value=_restore_value(payload["value"]),
                table_versions=payload["table_versions"],
                created_at=payload["created_at"],
            )
        except Exception:
            self.misses += 1
            return None
        self.hits += 1
        return entry

    def put(self, cache_key: str, value: Any, table_versions: dict[str, int], ttl_seconds: float | None = None):
        payload = {"value": value, "table_versions": table_versions, "created_at": time.time()}
        blob = zlib.compress(json.dumps(payload, default=_json_default).encode("utf-8"), level=1)
        ttl = int(ttl_seconds if ttl_seconds is not None else self._default_ttl)
        self._r.set(self._k(cache_key), blob, ex=ttl)

    def invalidate(self, cache_key: str):
        self._r.delete(self._k(cache_key))

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses, "hit_rate": self.hits / total if total else 0.0}
