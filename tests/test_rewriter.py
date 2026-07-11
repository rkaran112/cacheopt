from cacheopt.optimizer.rewriter import rewrite
from cacheopt.stats import StatsCatalog, TableStats


def make_stats():
    stats = StatsCatalog()
    stats._tables["fact_order_events"] = TableStats(row_count=12_000_000)
    stats._tables["dim_region"] = TableStats(row_count=24)
    stats._tables["dim_customer"] = TableStats(row_count=500_000)
    return stats


def test_constant_folding_evaluates_date_arithmetic():
    result = rewrite("SELECT 1 WHERE x >= date '2026-01-01' - interval 7 day", make_stats())
    assert "constant_fold" in result.applied
    assert "2025-12-25" in result.sql


def test_rollup_rewrite_redirects_matching_aggregate_query():
    sql = """SELECT region_id, date_trunc('day', event_time) as d, sum(revenue) as rev
             FROM fact_order_events
             WHERE event_time >= date '2026-01-01'
             GROUP BY region_id, date_trunc('day', event_time)"""
    result = rewrite(sql, make_stats())
    assert "rollup_rewrite" in result.applied
    assert "agg_daily_region_revenue" in result.sql
    assert "fact_order_events" not in result.sql


def test_rollup_rewrite_does_not_fire_on_unrelated_query():
    sql = "SELECT customer_id, sum(revenue) FROM fact_order_events GROUP BY customer_id"
    result = rewrite(sql, make_stats())
    assert "rollup_rewrite" not in result.applied


def test_join_reorder_puts_smallest_table_first():
    sql = """SELECT c.signup_year, sum(f.revenue)
             FROM fact_order_events f
             JOIN dim_customer c ON f.customer_id = c.customer_id
             JOIN dim_region r ON f.region_id = r.region_id
             GROUP BY 1"""
    result = rewrite(sql, make_stats())
    assert "join_reorder" in result.applied
    # dim_region (24 rows) should now be probed before dim_customer (500k rows)
    assert result.sql.index("dim_region") < result.sql.index("dim_customer")


def test_rewrite_never_crashes_on_garbage_input():
    result = rewrite("not valid sql {{{", make_stats())
    assert result.sql == "not valid sql {{{"
    assert result.applied == []
