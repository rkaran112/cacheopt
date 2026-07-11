"""Cost-based routing: decide, per query, whether to check L1/L2 at all,
which tier(s) to write a fresh result into, and what TTL to assign.

The model estimates an expected cost (in milliseconds) for candidate
execution paths and picks the cheapest, the same principle a classical
cost-based optimizer applies to join orders/access paths -- here applied one
level up, to *where a result should live* across the cache/storage
hierarchy rather than to how a single query executes inside the engine.

Inputs:
  * raw_cost_ms       -- estimated cost of computing the result from scratch
                         (L3 / DuckDB), from CardinalityEstimator.
  * hotness           -- recency+frequency score from AccessPatternTracker.
  * calibrated tier costs (cost_l1_ms, cost_l2_ms) -- measured once at
    startup against this machine/network, not guessed constants.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from ..config import Config
from ..stats import AccessPatternTracker, StatsCatalog
from ..storage.duckdb_backend import DuckDBBackend

ROWS_PER_MS_DEFAULT = 40_000.0  # overwritten by calibration


@dataclass
class RoutingPlan:
    check_l1: bool
    check_l2: bool
    write_l1: bool
    write_l2: bool
    ttl_seconds: float
    raw_cost_ms: float
    hotness: float
    reason: str


class CardinalityEstimator:
    """Turns a DuckDB EXPLAIN cardinality estimate + table stats into a
    millisecond cost estimate, calibrated against this machine's actual
    measured scan throughput rather than an arbitrary constant."""

    def __init__(self, backend: DuckDBBackend, stats_catalog: StatsCatalog):
        self._backend = backend
        self._stats = stats_catalog
        self.rows_per_ms = ROWS_PER_MS_DEFAULT

    def calibrate(self, calibration_table: str, calibration_sql: str | None = None):
        # Deliberately NOT `SELECT count(*)` -- DuckDB can answer that from
        # per-row-group metadata without touching column data, which would
        # make calibration wildly (and wrongly) optimistic. A full column
        # scan + aggregate forces genuine end-to-end read+compute cost, so
        # rows_per_ms reflects this machine's *real* scan throughput.
        numeric_col = None
        table_stats = self._stats.get(calibration_table)
        if table_stats and table_stats.column_min_max:
            numeric_col = next(iter(table_stats.column_min_max.keys()), None)
        sql = calibration_sql or (
            f"SELECT sum({numeric_col}) FROM {calibration_table}" if numeric_col
            else f"SELECT count(*) FROM {calibration_table} WHERE {calibration_table}.rowid IS NOT NULL"
        )
        start = time.perf_counter()
        self._backend.execute(sql)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        row_count = self._stats.get(calibration_table).row_count if self._stats.get(calibration_table) else None
        if row_count and elapsed_ms > 0:
            self.rows_per_ms = max(1000.0, row_count / elapsed_ms)

    # The column the fact table is physically clustered by (see
    # scripts/generate_dataset.py). Filters on this column benefit from
    # DuckDB's per-row-group zone maps (min/max pruning), so the planner's
    # own cardinality estimate is trustworthy for them. Filters on any other
    # column get no such benefit on this layout, and DuckDB's EXPLAIN
    # cardinality reflects *post-filter selectivity*, not *rows physically
    # read* -- so trusting it there would badly under-price a query that
    # actually has to scan the whole table to find a handful of matches.
    # This is a deliberate, documented simplification of a much deeper
    # problem (histogram/zonemap-aware cardinality estimation) that real
    # optimizers solve with far more machinery than is in scope here.
    CLUSTERED_COLUMN = "event_time"

    def _benefits_from_clustering(self, sql: str) -> bool:
        """True if the query's WHERE clause includes a predicate on the
        physically-clustered column. Zone-map pruning on that column still
        helps even when other, non-clustered predicates are also present
        (they're just applied to whatever survives the row-group skip), so
        presence -- not exclusivity -- is what matters here."""
        try:
            tree = sqlglot.parse_one(sql, read="duckdb")
        except Exception:
            return False
        where = tree.args.get("where") if isinstance(tree, exp.Select) else None
        if where is None:
            return False  # unfiltered -> genuinely a full scan
        cols = {c.name for c in where.find_all(exp.Column)}
        return self.CLUSTERED_COLUMN in cols

    def estimate_cost_ms(self, sql: str, tables: tuple[str, ...]) -> float:
        est_rows = self._backend.explain_cardinality(sql)
        if est_rows is None:
            est_rows = self._stats.total_rows(tables) or 100_000

        if not self._benefits_from_clustering(sql):
            full_scan_floor = self._stats.total_rows(tables) or est_rows
            est_rows = max(est_rows, full_scan_floor)

        fixed_overhead_ms = 0.8  # parse/plan/dispatch overhead, roughly measured
        return fixed_overhead_ms + (est_rows / self.rows_per_ms)


class CostModel:
    def __init__(self, config: Config, estimator: CardinalityEstimator, access_tracker: AccessPatternTracker,
                 max_cacheable_rows: int = 200_000):
        self._config = config
        self._estimator = estimator
        self._tracker = access_tracker
        self._max_cacheable_rows = max_cacheable_rows

    def plan(self, sql: str, template_id: str, tables: tuple[str, ...]) -> RoutingPlan:
        raw_cost_ms = self._estimator.estimate_cost_ms(sql, tables)
        hotness = self._tracker.hotness(template_id)
        result_rows = self._estimator._backend.explain_result_cardinality(sql)

        if raw_cost_ms < self._config.cache_admission_min_cost_ms:
            return RoutingPlan(False, False, False, False, 0.0, raw_cost_ms, hotness,
                                reason="cheap-to-recompute: caching overhead not justified")

        if result_rows is not None and result_rows > self._max_cacheable_rows:
            return RoutingPlan(True, True, False, False, 0.0, raw_cost_ms, hotness,
                                reason="result set too large to cache economically; still consult cache in case of prior smaller run")

        # p_hit heuristic: saturating function of hotness (0 -> never seen, grows toward 1)
        p_hit = 1.0 - pow(2.718281828, -hotness)

        write_l1 = p_hit > 0.35  # "hot": worth the local-node memory budget
        write_l2 = True          # always worth sharing a non-trivial result across nodes

        ttl = self._config.default_ttl_seconds * (2.0 if write_l1 else 1.0)

        reason = (
            f"raw_cost={raw_cost_ms:.2f}ms hotness={hotness:.2f} p_hit~={p_hit:.2f} -> "
            f"{'L1+L2' if write_l1 else 'L2-only'} placement"
        )
        return RoutingPlan(True, True, write_l1, write_l2, ttl, raw_cost_ms, hotness, reason)
