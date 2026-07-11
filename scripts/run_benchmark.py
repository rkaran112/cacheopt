"""Benchmark runner.

Run in three stages (kept separate so each fits comfortably in a bounded
time budget):

    python scripts/run_benchmark.py --stage baseline  --n 500 --out benchmarks/results
    python scripts/run_benchmark.py --stage optimized --n 500 --out benchmarks/results
    python scripts/run_benchmark.py --stage report     --out benchmarks/results

Both the baseline and optimized stages replay the *identical* workload
(same seed -> generate_workload() is deterministic), so every query at
position k is the same SQL in both runs. That is what makes "latency
reduction for repeat workloads" a fair, apples-to-apples number instead of
comparing two different traffic mixes.

  * baseline  -- every query executes directly against DuckDB. No cache,
    no rewriter, no cost model. This is "what it looked like before."
  * optimized -- every query goes through a 3-node EngineCluster (the full
    fingerprint -> rewrite -> cost model -> tiered cache -> DuckDB path).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from query_templates import generate_workload, TEMPLATES  # noqa: E402

from cacheopt.config import Config  # noqa: E402
from cacheopt.engine import EngineCluster  # noqa: E402
from cacheopt.storage.duckdb_backend import DuckDBBackend  # noqa: E402


def _hot_set():
    """Every (template, sql) pair that came from a hot-param combo, so we
    can classify each workload item as repeat-traffic vs cold/ad hoc after
    the fact without re-deriving randomness."""
    hot = set()
    for t in TEMPLATES:
        for params in t.hot_params:
            hot.add(t.sql_fn(*params).strip())
    return hot


def run_baseline(db_path: str, n: int, p_hot: float, seed: int) -> list[dict]:
    backend = DuckDBBackend(db_path, read_only=True)
    workload = generate_workload(n, p_hot=p_hot, seed=seed)
    hot_sqls = _hot_set()
    results = []
    for template_name, sql in workload:
        t0 = time.perf_counter()
        r = backend.execute(sql)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        results.append({
            "template": template_name,
            "is_hot": sql.strip() in hot_sqls,
            "latency_ms": latency_ms,
            "rows": len(r.rows),
        })
    backend.close()
    return results


def run_optimized(db_path: str, n: int, p_hot: float, seed: int, num_nodes: int = 3) -> tuple[list[dict], dict]:
    cfg = dataclasses.replace(
        Config(),
        duckdb_path=db_path,
        duckdb_read_only=False,  # writers touch the version-tracking Redis, not the file, but keep flexible
        redis_mode="embedded",
        redis_rdb_path="data/bench_redis.rdb",
    )
    cluster = EngineCluster(cfg, num_nodes=num_nodes)
    cluster.refresh_stats(
        ["fact_order_events", "dim_customer", "dim_product", "dim_region", "agg_daily_region_revenue"],
        sample_columns={"fact_order_events": ["region_id", "customer_id", "product_id"]},
    )

    workload = generate_workload(n, p_hot=p_hot, seed=seed)
    hot_sqls = _hot_set()
    results = []
    rewrite_counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}
    for template_name, sql in workload:
        node = cluster.route()
        result = node.execute(sql)
        for rule in result.rewrites_applied:
            rewrite_counts[rule] = rewrite_counts.get(rule, 0) + 1
        tier_counts[result.tier_hit.value] = tier_counts.get(result.tier_hit.value, 0) + 1
        results.append({
            "template": template_name,
            "is_hot": sql.strip() in hot_sqls,
            "latency_ms": result.latency_ms,
            "tier_hit": result.tier_hit.value,
            "rewrites": result.rewrites_applied,
        })

    node_stats = [node.stats() for node in cluster.nodes]
    meta = {"rewrite_counts": rewrite_counts, "tier_counts": tier_counts, "node_stats": node_stats}
    cluster.close()
    return results, meta


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p / 100 * (len(s) - 1)))))
    return s[k]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True, choices=["baseline", "optimized", "report"])
    parser.add_argument("--db", default="data/warehouse2.duckdb")
    parser.add_argument("--n", type=int, default=500)
    parser.add_argument("--p-hot", type=float, default=0.88)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--nodes", type=int, default=3)
    parser.add_argument("--out", default="benchmarks/results")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    if args.stage == "baseline":
        t0 = time.time()
        results = run_baseline(args.db, args.n, args.p_hot, args.seed)
        with open(os.path.join(args.out, "baseline.json"), "w") as f:
            json.dump({"config": vars(args), "results": results}, f)
        print(f"baseline: {len(results)} queries in {time.time()-t0:.1f}s")

    elif args.stage == "optimized":
        t0 = time.time()
        results, meta = run_optimized(args.db, args.n, args.p_hot, args.seed, args.nodes)
        with open(os.path.join(args.out, "optimized.json"), "w") as f:
            json.dump({"config": vars(args), "results": results, "meta": meta}, f)
        print(f"optimized: {len(results)} queries in {time.time()-t0:.1f}s")

    elif args.stage == "report":
        with open(os.path.join(args.out, "baseline.json")) as f:
            baseline = json.load(f)
        with open(os.path.join(args.out, "optimized.json")) as f:
            optimized = json.load(f)

        base_results = baseline["results"]
        opt_results = optimized["results"]
        assert len(base_results) == len(opt_results), "baseline/optimized workloads must match 1:1"

        base_latencies = [r["latency_ms"] for r in base_results]
        opt_latencies = [r["latency_ms"] for r in opt_results]

        base_hot = [r["latency_ms"] for r in base_results if r["is_hot"]]
        opt_hot = [r["latency_ms"] for r in opt_results if r["is_hot"]]

        summary = {
            "n_queries": len(base_results),
            "p_hot": optimized["config"]["p_hot"],
            "baseline": {
                "mean_ms": statistics.mean(base_latencies),
                "p50_ms": percentile(base_latencies, 50),
                "p95_ms": percentile(base_latencies, 95),
                "p99_ms": percentile(base_latencies, 99),
            },
            "optimized": {
                "mean_ms": statistics.mean(opt_latencies),
                "p50_ms": percentile(opt_latencies, 50),
                "p95_ms": percentile(opt_latencies, 95),
                "p99_ms": percentile(opt_latencies, 99),
            },
            "repeat_workload": {
                "n_repeat_queries": len(base_hot),
                "baseline_mean_ms": statistics.mean(base_hot) if base_hot else None,
                "optimized_mean_ms": statistics.mean(opt_hot) if opt_hot else None,
                "latency_reduction_pct": (
                    100.0 * (1 - statistics.mean(opt_hot) / statistics.mean(base_hot))
                    if base_hot and statistics.mean(base_hot) > 0 else None
                ),
            },
            "cache_tier_distribution": optimized["meta"]["tier_counts"],
            "rewrite_rule_activations": optimized["meta"]["rewrite_counts"],
            "overall_speedup_x": statistics.mean(base_latencies) / statistics.mean(opt_latencies),
        }

        with open(os.path.join(args.out, "summary.json"), "w") as f:
            json.dump(summary, f, indent=2)

        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
