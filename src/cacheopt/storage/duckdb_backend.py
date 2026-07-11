"""L3: persistent storage backend.

DuckDB is used as the durable, on-disk analytical engine underneath the
cache tiers -- a genuine embedded OLAP query engine (columnar storage,
vectorized execution), not a toy in-memory stand-in. Every cache miss
ultimately falls through to here.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass

import duckdb


@dataclass
class ExecResult:
    columns: tuple[str, ...]
    rows: list[tuple]
    latency_ms: float
    estimated_cardinality: int | None = None


class DuckDBBackend:
    """Thread-safe wrapper around a single DuckDB database file.

    DuckDB connections are not free-threaded, so writes/reads from multiple
    simulated "nodes" in the benchmark serialize through a lock here -- this
    mirrors how a real deployment would front a shared analytical store
    (e.g. a Redshift/BigQuery/Snowflake cluster) with many stateless query
    engine nodes hitting it concurrently.
    """

    def __init__(self, path: str, read_only: bool = False):
        self.path = path
        self._conn = duckdb.connect(path, read_only=read_only)
        self._lock = threading.Lock()

    def execute(self, sql: str, params: list | None = None) -> ExecResult:
        start = time.perf_counter()
        with self._lock:
            cur = self._conn.execute(sql, params) if params else self._conn.execute(sql)
            rows = cur.fetchall()
            columns = tuple(d[0] for d in cur.description) if cur.description else ()
        latency_ms = (time.perf_counter() - start) * 1000.0
        return ExecResult(columns=columns, rows=rows, latency_ms=latency_ms)

    def _explain_json(self, sql: str) -> dict | None:
        try:
            with self._lock:
                plan_rows = self._conn.execute(f"EXPLAIN (FORMAT JSON) {sql}").fetchall()
        except Exception:
            return None
        for row in plan_rows:
            for cell in row:
                if isinstance(cell, str) and cell.strip().startswith("["):
                    try:
                        data = json.loads(cell)
                        return data[0] if isinstance(data, list) and data else None
                    except Exception:
                        return None
        return None

    def explain_cardinality(self, sql: str) -> int | None:
        """Best-effort estimated *work* (max cardinality across every plan
        node, typically dominated by the largest base-table scan), used by
        the cost model as a proxy for how expensive a query is to compute
        without actually running it."""
        root = self._explain_json(sql)
        if root is None:
            return None
        return _find_max_cardinality(root)

    def explain_result_cardinality(self, sql: str) -> int | None:
        """Best-effort estimated size of the *final result set* (the
        top-level plan node's own cardinality), used to decide whether a
        result is small enough to be worth caching. This is deliberately
        different from explain_cardinality: a GROUP BY over 10M input rows
        that emits 50 output rows should be cached even though it does a
        lot of work, and this is the number that reflects "output rows"."""
        root = self._explain_json(sql)
        if root is None:
            return None
        return _own_cardinality(root)

    def table_names(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SHOW TABLES").fetchall()
        return [r[0] for r in rows]

    def raw_connection(self):
        return self._conn

    def close(self):
        self._conn.close()


def _own_cardinality(node: dict) -> int | None:
    extra = node.get("extra_info", {})
    val = extra.get("Estimated Cardinality")
    try:
        return int(val) if val is not None else None
    except (TypeError, ValueError):
        return None


def _find_max_cardinality(node) -> int | None:
    best = None
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            extra = cur.get("extra_info")
            if isinstance(extra, dict) and "Estimated Cardinality" in extra:
                try:
                    best = max(best or 0, int(extra["Estimated Cardinality"]))
                except (TypeError, ValueError):
                    pass
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return best
