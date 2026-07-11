"""Statistics: table/column catalog stats + per-query access-pattern tracking.

Two distinct kinds of statistics feed the cost model, mirroring the split in
a real cost-based optimizer (e.g. Postgres pg_statistic vs. pg_stat_statements):

1. `TableStats` -- cardinality / catalog information about the base data
   (row counts, approximate distinct values). Used to estimate the *raw*
   cost of executing a query against L3 if nothing is cached.

2. `AccessPatternTracker` -- runtime statistics about how often each query
   *template* is seen and how expensive it has historically been. Used to
   estimate cache hit probability and decide tier placement (the "adaptive"
   part of the adaptive caching strategy).
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class TableStats:
    row_count: int
    approx_distinct: dict[str, int] = field(default_factory=dict)
    column_min_max: dict[str, tuple] = field(default_factory=dict)


class StatsCatalog:
    """Holds TableStats for every table, refreshed from DuckDB's own catalog."""

    def __init__(self):
        self._tables: dict[str, TableStats] = {}
        self._lock = threading.Lock()

    def refresh_from_duckdb(self, conn, tables: list[str], sample_columns: dict[str, list[str]] | None = None):
        sample_columns = sample_columns or {}
        with self._lock:
            for table in tables:
                row_count = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                distinct = {}
                minmax = {}
                for col in sample_columns.get(table, []):
                    d = conn.execute(f"SELECT approx_count_distinct({col}) FROM {table}").fetchone()[0]
                    distinct[col] = int(d)
                    lo, hi = conn.execute(f"SELECT min({col}), max({col}) FROM {table}").fetchone()
                    minmax[col] = (lo, hi)
                self._tables[table] = TableStats(row_count=row_count, approx_distinct=distinct, column_min_max=minmax)

    def get(self, table: str) -> TableStats | None:
        return self._tables.get(table)

    def total_rows(self, tables: tuple[str, ...]) -> int:
        return sum(self._tables[t].row_count for t in tables if t in self._tables)


@dataclass
class TemplateStats:
    """Rolling stats for one query template (a query "shape")."""
    count: int = 0
    last_seen: float = 0.0
    ewma_interarrival_s: float | None = None
    latency_history_ms: deque = field(default_factory=lambda: deque(maxlen=200))

    def observe(self, now: float, latency_ms: float):
        if self.count > 0:
            gap = now - self.last_seen
            if self.ewma_interarrival_s is None:
                self.ewma_interarrival_s = gap
            else:
                alpha = 0.3
                self.ewma_interarrival_s = alpha * gap + (1 - alpha) * self.ewma_interarrival_s
        self.count += 1
        self.last_seen = now
        self.latency_history_ms.append(latency_ms)

    def p95_latency_ms(self) -> float | None:
        if not self.latency_history_ms:
            return None
        s = sorted(self.latency_history_ms)
        idx = min(len(s) - 1, math.ceil(0.95 * len(s)) - 1)
        return s[idx]

    def avg_latency_ms(self) -> float | None:
        if not self.latency_history_ms:
            return None
        return sum(self.latency_history_ms) / len(self.latency_history_ms)


class AccessPatternTracker:
    """Tracks frequency + recency per query template and derives a hotness
    score used by the cost model to decide cache tier placement.

    hotness(t) = frequency_component * recency_decay

    frequency_component grows with log(count) (diminishing returns, so one
    query seen 10,000 times doesn't dominate forever) and recency_decay is an
    exponential decay based on time since last access, with a configurable
    half-life. This is a simplified version of the frequency+recency scoring
    used by adaptive cache-replacement policies like ARC / W-TinyLFU.
    """

    def __init__(self, half_life_seconds: float = 60.0, history_window: int = 200):
        self._half_life = half_life_seconds
        self._history_window = history_window
        self._templates: dict[str, TemplateStats] = {}
        self._lock = threading.Lock()

    def record(self, template_id: str, latency_ms: float, now: float | None = None) -> TemplateStats:
        now = now if now is not None else time.time()
        with self._lock:
            ts = self._templates.setdefault(template_id, TemplateStats(latency_history_ms=deque(maxlen=self._history_window)))
            ts.observe(now, latency_ms)
            return ts

    def hotness(self, template_id: str, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        with self._lock:
            ts = self._templates.get(template_id)
            if ts is None or ts.count == 0:
                return 0.0
            freq_component = math.log1p(ts.count)
            age = max(0.0, now - ts.last_seen)
            decay = math.exp(-math.log(2) * age / self._half_life)
            return freq_component * decay

    def get(self, template_id: str) -> TemplateStats | None:
        return self._templates.get(template_id)

    def snapshot(self) -> dict[str, TemplateStats]:
        with self._lock:
            return dict(self._templates)
