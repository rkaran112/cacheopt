"""Distributed execution planner.

Ties together fingerprinting, statistics, the rewriter, the cost model, and
the tiered cache into a single request path. "Distributed" here refers to
the fact that this planner runs identically on every query-engine node, with
each node holding its own local L1 buffer while sharing the L2 (Redis) and
L3 (DuckDB) tiers -- so any node's cache warm-up benefits every other node
through L2, which is the actual distributed-caching behavior being
exercised in the benchmark.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..cache.tiered_cache import TieredCache, TierHit
from ..fingerprint import fingerprint
from ..stats import AccessPatternTracker, StatsCatalog
from ..storage.duckdb_backend import DuckDBBackend
from .cost_model import CostModel
from .rewriter import rewrite


@dataclass
class QueryResult:
    columns: tuple[str, ...]
    rows: list[tuple]
    latency_ms: float
    tier_hit: TierHit
    rewrites_applied: list[str] = field(default_factory=list)
    routing_reason: str = ""
    template_id: str = ""


class DistributedExecutionPlanner:
    def __init__(self, backend: DuckDBBackend, tiered_cache: TieredCache, cost_model: CostModel,
                 stats_catalog: StatsCatalog, access_tracker: AccessPatternTracker, dialect: str = "duckdb"):
        self._backend = backend
        self._cache = tiered_cache
        self._cost_model = cost_model
        self._stats = stats_catalog
        self._tracker = access_tracker
        self._dialect = dialect

    def execute(self, sql: str) -> QueryResult:
        overall_start = time.perf_counter()

        fp = fingerprint(sql, dialect=self._dialect)
        rewrite_result = rewrite(sql, self._stats, dialect=self._dialect)
        effective_sql = rewrite_result.sql
        effective_fp = fingerprint(effective_sql, dialect=self._dialect) if rewrite_result.applied else fp

        plan = self._cost_model.plan(effective_sql, effective_fp.template_id, effective_fp.tables)

        lookup = self._cache.get(
            effective_fp.cache_key, effective_fp.tables,
            check_l1=plan.check_l1, check_l2=plan.check_l2,
        )

        if lookup.hit != TierHit.MISS:
            columns, rows = lookup.value
            latency_ms = (time.perf_counter() - overall_start) * 1000.0
            self._tracker.record(effective_fp.template_id, latency_ms)
            return QueryResult(columns=columns, rows=rows, latency_ms=latency_ms, tier_hit=lookup.hit,
                                rewrites_applied=rewrite_result.applied, routing_reason=plan.reason,
                                template_id=effective_fp.template_id)

        exec_result = self._backend.execute(effective_sql)

        if plan.write_l1 or plan.write_l2:
            self._cache.put(
                effective_fp.cache_key, (exec_result.columns, exec_result.rows), effective_fp.tables,
                write_l1=plan.write_l1, write_l2=plan.write_l2, ttl_seconds=plan.ttl_seconds,
            )

        latency_ms = (time.perf_counter() - overall_start) * 1000.0
        self._tracker.record(effective_fp.template_id, latency_ms)
        return QueryResult(columns=exec_result.columns, rows=exec_result.rows, latency_ms=latency_ms,
                            tier_hit=TierHit.MISS, rewrites_applied=rewrite_result.applied,
                            routing_reason=plan.reason, template_id=effective_fp.template_id)
