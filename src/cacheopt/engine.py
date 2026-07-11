"""Top-level public API: `QueryEngine` represents one node in the
distributed fleet.

Typical usage:

    engine = QueryEngine.shared_node(config)   # one of N nodes, all sharing
                                                # the same Redis + DuckDB file
    result = engine.execute("SELECT ...")
    engine.write("fact_order_events", "INSERT INTO fact_order_events VALUES (...)")

Multiple `QueryEngine` instances created against the same Config (or
explicitly sharing a DuckDBBackend + Redis client) simulate multiple
application-server nodes in a fleet: each gets its own local L1 buffer, but
they share L2 (Redis) and L3 (DuckDB), which is what makes cache warm-up on
one node visible to the others -- the "distributed" in distributed
cache-aware.
"""
from __future__ import annotations

import threading

from .cache.memory_buffer import MemoryBuffer
from .cache.redis_cache import RedisCache
from .cache.redis_client import get_redis_client
from .cache.tiered_cache import TieredCache
from .config import Config
from .invalidation import InvalidationSubscriber, TableVersionManager
from .optimizer.cost_model import CardinalityEstimator, CostModel
from .optimizer.planner import DistributedExecutionPlanner, QueryResult
from .stats import AccessPatternTracker, StatsCatalog
from .storage.duckdb_backend import DuckDBBackend


class QueryEngine:
    def __init__(self, config: Config, backend: DuckDBBackend, redis_client, stats_catalog: StatsCatalog,
                 node_id: str = "node-0"):
        self.config = config
        self.node_id = node_id
        self.backend = backend
        self.stats_catalog = stats_catalog

        self.memory = MemoryBuffer(max_entries=config.l1_max_entries, max_bytes=config.l1_max_bytes)
        self.redis_cache = RedisCache(redis_client, default_ttl_seconds=config.default_ttl_seconds)
        self.version_mgr = TableVersionManager(redis_client)
        self.tiered_cache = TieredCache(self.memory, self.redis_cache, self.version_mgr)

        self.access_tracker = AccessPatternTracker(
            half_life_seconds=config.hotness_half_life_seconds,
            history_window=config.history_window,
        )
        self.estimator = CardinalityEstimator(backend, stats_catalog)
        self.cost_model = CostModel(config, self.estimator, self.access_tracker,
                                     max_cacheable_rows=config.max_cacheable_rows)
        self.planner = DistributedExecutionPlanner(backend, self.tiered_cache, self.cost_model,
                                                     stats_catalog, self.access_tracker)

        self._subscriber = InvalidationSubscriber(redis_client, self._on_invalidate)
        self._subscriber.start()

    def _on_invalidate(self, tables: set[str], versions: dict[str, int]):
        self.memory.invalidate_tables(tables, versions)

    def execute(self, sql: str) -> QueryResult:
        return self.planner.execute(sql)

    def write(self, table: str, dml_sql: str):
        """Apply a write against the persistent store, then atomically bump
        the table's version and publish invalidation so every node (this one
        included) drops now-stale cache entries for that table."""
        self.backend.execute(dml_sql)
        return self.version_mgr.bump_and_publish(table)

    def calibrate(self, calibration_table: str):
        self.estimator.calibrate(calibration_table)

    def stats(self) -> dict:
        return {
            "node_id": self.node_id,
            "cache": self.tiered_cache.stats(),
            "templates_tracked": len(self.access_tracker.snapshot()),
        }

    def close(self):
        self._subscriber.stop()


class EngineCluster:
    """Convenience wrapper: spins up N QueryEngine nodes sharing one Redis
    client and one DuckDB backend, for simulating a distributed fleet in
    tests and benchmarks."""

    def __init__(self, config: Config, num_nodes: int = 3):
        self.config = config
        self.backend = DuckDBBackend(config.duckdb_path, read_only=config.duckdb_read_only)
        self.redis_client = get_redis_client(config)
        self.stats_catalog = StatsCatalog()
        self.nodes: list[QueryEngine] = [
            QueryEngine(config, self.backend, self.redis_client, self.stats_catalog, node_id=f"node-{i}")
            for i in range(num_nodes)
        ]
        self._rr_lock = threading.Lock()
        self._rr_index = 0

    def refresh_stats(self, tables: list[str], sample_columns: dict[str, list[str]] | None = None):
        self.stats_catalog.refresh_from_duckdb(self.backend.raw_connection(), tables, sample_columns)
        for node in self.nodes:
            node.calibrate(tables[0])

    def route(self) -> QueryEngine:
        """Round-robin node selection, simulating a load balancer."""
        with self._rr_lock:
            node = self.nodes[self._rr_index % len(self.nodes)]
            self._rr_index += 1
            return node

    def close(self):
        for node in self.nodes:
            node.close()
        self.backend.close()
