"""FastAPI layer exposing the QueryEngine/EngineCluster to a browser client.

This is the only place in the project that accepts SQL from an untrusted
caller, so it leans on the read-only-SELECT guard already enforced in
QueryEngine.execute() (see engine.py) rather than re-implementing it here --
one validation boundary, not two that can drift out of sync.
"""
from __future__ import annotations

import dataclasses
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from cacheopt.config import Config
from cacheopt.engine import EngineCluster

FACT_TABLES = ["fact_order_events", "dim_customer", "dim_product", "dim_region", "agg_daily_region_revenue"]

SAMPLE_QUERIES = [
    {
        "name": "Revenue by region (last 7 days)",
        "sql": (
            "SELECT region_id, sum(revenue) AS total_revenue, count(*) AS n\n"
            "FROM fact_order_events\n"
            "WHERE event_time >= TIMESTAMP '2026-07-05' - INTERVAL 7 DAY\n"
            "  AND event_type = 'purchase'\n"
            "GROUP BY region_id\n"
            "ORDER BY total_revenue DESC"
        ),
    },
    {
        "name": "Daily revenue rollup, region 0 (last 30 days)",
        "sql": (
            "SELECT region_id, date_trunc('day', event_time) AS d, sum(revenue) AS rev\n"
            "FROM fact_order_events\n"
            "WHERE event_time >= TIMESTAMP '2026-07-05' - INTERVAL 30 DAY\n"
            "  AND region_id = 0\n"
            "GROUP BY region_id, date_trunc('day', event_time)"
        ),
    },
    {
        "name": "Top 10 products by revenue (last 14 days)",
        "sql": (
            "SELECT p.product_id, p.category, sum(f.revenue) AS total_revenue\n"
            "FROM fact_order_events f\n"
            "JOIN dim_product p ON f.product_id = p.product_id\n"
            "WHERE f.event_time >= TIMESTAMP '2026-07-05' - INTERVAL 14 DAY\n"
            "GROUP BY p.product_id, p.category\n"
            "ORDER BY total_revenue DESC\n"
            "LIMIT 10"
        ),
    },
]


class QueryRequest(BaseModel):
    sql: str


app = FastAPI(title="cacheopt API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("CACHEOPT_FRONTEND_ORIGIN", "http://localhost:5173")],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

_cluster: EngineCluster | None = None


@app.on_event("startup")
def _startup():
    global _cluster
    cfg = dataclasses.replace(Config(), duckdb_read_only=False)
    cluster = EngineCluster(cfg, num_nodes=3)
    cluster.refresh_stats(
        FACT_TABLES,
        sample_columns={"fact_order_events": ["region_id", "customer_id", "product_id"]},
    )
    _cluster = cluster


@app.on_event("shutdown")
def _shutdown():
    if _cluster is not None:
        _cluster.close()


def _cluster_or_503() -> EngineCluster:
    if _cluster is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    return _cluster


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/samples")
def samples():
    return SAMPLE_QUERIES


@app.post("/api/query")
def run_query(req: QueryRequest):
    cluster = _cluster_or_503()
    node = cluster.route()
    t0 = time.perf_counter()
    try:
        result = node.execute(req.sql)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"query failed: {e}") from e
    wall_ms = (time.perf_counter() - t0) * 1000.0

    return {
        "node_id": node.node_id,
        "tier_hit": result.tier_hit.value,
        "latency_ms": result.latency_ms,
        "wall_ms": wall_ms,
        "rewrites_applied": result.rewrites_applied,
        "routing_reason": result.routing_reason,
        "template_id": result.template_id,
        "columns": list(result.columns),
        "rows": [list(r) for r in result.rows[:200]],
        "row_count": len(result.rows),
        "truncated": len(result.rows) > 200,
    }


@app.get("/api/stats")
def stats():
    cluster = _cluster_or_503()
    return {
        "nodes": [node.stats() for node in cluster.nodes],
    }
