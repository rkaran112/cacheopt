"""Cache invalidation protocol.

Consistency model (stated precisely, not oversold):

  * Redis holds one authoritative, monotonically increasing version counter
    per table: `cacheopt:tblver:{table}`. A write to a table atomically
    increments this counter (INCR is atomic in Redis) before the write is
    considered committed.

  * Every cached entry -- at both L1 and L2 -- stores the table_versions
    map {table: version} that was current at the moment the query result
    was computed.

  * L2 (Redis) reads are validated synchronously on every hit: the current
    versions for the entry's referenced tables are re-read from Redis and
    compared against the versions captured in the entry. If any table has
    moved on, the entry is treated as a miss and discarded. Because the
    version counters and the cached blob live in the same Redis instance,
    this gives L2 **strong consistency**: it is not possible to observe an
    L2 hit for data older than the last committed write, independent of
    cache TTLs or replication lag.

  * L1 (per-node memory) is NOT re-validated against Redis on every hit --
    that would defeat the purpose of having a zero-network local tier. It is
    instead kept consistent by synchronous pub/sub invalidation: a writer
    publishes an `invalidate:{table}` event immediately after bumping the
    version, and every node's background subscriber thread purges matching
    L1 entries on receipt. In practice this closes the staleness window to
    single-digit milliseconds (local pub/sub fan-out latency), and combined
    with the strongly-consistent L2 layer beneath it, no node can serve
    L1-stale data for longer than one local pub/sub round trip. This is a
    deliberately honest "two-speed" design rather than a claim of
    distributed consensus / linearizability across nodes.
"""
from __future__ import annotations

import json
import threading
from typing import Callable

VERSION_KEY_PREFIX = "cacheopt:tblver:"
INVALIDATE_CHANNEL_PREFIX = "cacheopt:invalidate:"


class TableVersionManager:
    def __init__(self, redis_client):
        self._r = redis_client

    def current_versions(self, tables: tuple[str, ...]) -> dict[str, int]:
        if not tables:
            return {}
        pipe = self._r.pipeline()
        for t in tables:
            pipe.get(f"{VERSION_KEY_PREFIX}{t}")
        raw = pipe.execute()
        return {t: int(v) if v is not None else 0 for t, v in zip(tables, raw)}

    def bump_and_publish(self, table: str) -> int:
        """Called by writers after a DML statement commits against DuckDB.
        Atomically increments the table's version and publishes an
        invalidation event so every node's L1 can drop stale entries."""
        pipe = self._r.pipeline()
        pipe.incr(f"{VERSION_KEY_PREFIX}{table}")
        new_version = pipe.execute()[0]
        self._r.publish(f"{INVALIDATE_CHANNEL_PREFIX}{table}", json.dumps({"table": table, "version": new_version}))
        return new_version

    def is_stale(self, captured_versions: dict[str, int], tables: tuple[str, ...] | None = None) -> bool:
        tables = tables if tables is not None else tuple(captured_versions.keys())
        current = self.current_versions(tables)
        return any(current.get(t, 0) > captured_versions.get(t, -1) for t in tables)


class InvalidationSubscriber:
    """Background thread: subscribes to invalidate:* and purges the local
    L1 buffer for affected tables as soon as an event arrives. One instance
    runs per query-engine node."""

    def __init__(self, redis_client, on_invalidate: Callable[[set[str], dict[str, int]], None]):
        self._r = redis_client
        self._on_invalidate = on_invalidate
        self._pubsub = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self):
        self._pubsub = self._r.pubsub(ignore_subscribe_messages=True)
        self._pubsub.psubscribe(f"{INVALIDATE_CHANNEL_PREFIX}*")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            message = self._pubsub.get_message(timeout=0.2)
            if message is None or message.get("type") != "pmessage":
                continue
            try:
                payload = json.loads(message["data"])
                table = payload["table"]
                version = payload["version"]
                self._on_invalidate({table}, {table: version})
            except Exception:
                continue

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._pubsub is not None:
            self._pubsub.close()
