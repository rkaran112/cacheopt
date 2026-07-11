import dataclasses

from cacheopt.optimizer.cost_model import CardinalityEstimator, CostModel
from cacheopt.stats import AccessPatternTracker, StatsCatalog
from cacheopt.storage.duckdb_backend import DuckDBBackend


def make_backend(tmp_path):
    backend = DuckDBBackend(str(tmp_path / "cost.duckdb"))
    backend.execute("CREATE TABLE t AS SELECT i AS id, i % 100 AS grp FROM range(500000) t(i)")
    return backend


def test_cheap_query_bypasses_caching(tmp_path):
    backend = make_backend(tmp_path)
    stats = StatsCatalog()
    stats.refresh_from_duckdb(backend.raw_connection(), ["t"])
    estimator = CardinalityEstimator(backend, stats)
    estimator.calibrate("t")
    tracker = AccessPatternTracker()
    from cacheopt.config import Config
    cfg = dataclasses.replace(Config(), cache_admission_min_cost_ms=10_000.0)  # force "cheap" classification
    model = CostModel(cfg, estimator, tracker)

    plan = model.plan("SELECT 1", "tmpl1", ("t",))
    assert plan.write_l1 is False and plan.write_l2 is False
    assert "cheap-to-recompute" in plan.reason
    backend.close()


def test_repeated_query_becomes_hot_and_gets_l1_placement(tmp_path):
    backend = make_backend(tmp_path)
    stats = StatsCatalog()
    stats.refresh_from_duckdb(backend.raw_connection(), ["t"])
    estimator = CardinalityEstimator(backend, stats)
    estimator.calibrate("t")
    tracker = AccessPatternTracker(half_life_seconds=60.0)
    from cacheopt.config import Config
    cfg = dataclasses.replace(Config(), cache_admission_min_cost_ms=0.0)
    model = CostModel(cfg, estimator, tracker)

    sql = "SELECT grp, count(*) FROM t GROUP BY grp"
    first_plan = model.plan(sql, "tmpl2", ("t",))
    assert first_plan.write_l1 is False  # never seen before -> not hot yet

    for _ in range(10):
        tracker.record("tmpl2", latency_ms=5.0)

    later_plan = model.plan(sql, "tmpl2", ("t",))
    assert later_plan.write_l1 is True  # now hot enough to warrant local placement
    backend.close()


def test_large_result_set_skips_caching(tmp_path):
    backend = make_backend(tmp_path)
    stats = StatsCatalog()
    stats.refresh_from_duckdb(backend.raw_connection(), ["t"])
    estimator = CardinalityEstimator(backend, stats)
    estimator.calibrate("t")
    tracker = AccessPatternTracker()
    from cacheopt.config import Config
    cfg = dataclasses.replace(Config(), cache_admission_min_cost_ms=0.0)
    model = CostModel(cfg, estimator, tracker, max_cacheable_rows=1000)

    plan = model.plan("SELECT * FROM t", "tmpl3", ("t",))
    assert plan.write_l1 is False and plan.write_l2 is False
    assert "too large" in plan.reason
    backend.close()
