import time

from cacheopt.cache.tiered_cache import TierHit
from cacheopt.engine import EngineCluster


def _seed(cluster):
    conn = cluster.backend.raw_connection()
    conn.execute("""
        CREATE TABLE fact_order_events AS
        SELECT i AS id, (i % 24) AS region_id, (i % 100)::DOUBLE AS revenue,
               TIMESTAMP '2026-01-01' + INTERVAL (i % 30) DAY AS event_time
        FROM range(300000) t(i)
    """)
    cluster.refresh_stats(["fact_order_events"])


def test_first_execution_is_a_miss_and_populates_cache(config):
    cluster = EngineCluster(config, num_nodes=1)
    try:
        _seed(cluster)
        node = cluster.route()
        result = node.execute("SELECT region_id, sum(revenue) FROM fact_order_events GROUP BY region_id")
        assert result.tier_hit == TierHit.MISS
        assert len(result.rows) == 24
    finally:
        cluster.close()


def test_second_execution_hits_cache_and_is_faster(config):
    cluster = EngineCluster(config, num_nodes=1)
    try:
        _seed(cluster)
        node = cluster.route()
        sql = "SELECT region_id, sum(revenue) FROM fact_order_events GROUP BY region_id"
        first = node.execute(sql)
        second = node.execute(sql)
        assert first.tier_hit == TierHit.MISS
        assert second.tier_hit != TierHit.MISS
        assert second.rows == first.rows
        assert second.latency_ms < first.latency_ms
    finally:
        cluster.close()


def test_cache_shared_across_nodes_in_cluster(config):
    cluster = EngineCluster(config, num_nodes=3)
    try:
        _seed(cluster)
        sql = "SELECT region_id, sum(revenue) FROM fact_order_events GROUP BY region_id"
        node0, node1, node2 = cluster.nodes
        r0 = node0.execute(sql)
        r1 = node1.execute(sql)  # different node, same shared L2
        assert r0.tier_hit == TierHit.MISS
        assert r1.tier_hit == TierHit.L2
        assert r1.rows == r0.rows
    finally:
        cluster.close()


def test_write_invalidates_cached_result_cluster_wide(config):
    cluster = EngineCluster(config, num_nodes=2)
    try:
        _seed(cluster)
        sql = "SELECT sum(revenue) FROM fact_order_events WHERE region_id = 0"
        node0, node1 = cluster.nodes
        before = node0.execute(sql)
        node1.execute(sql)  # warm node1's cache too

        node0.write("fact_order_events", "UPDATE fact_order_events SET revenue = revenue + 100 WHERE region_id = 0")
        time.sleep(0.3)  # allow pub/sub invalidation to propagate to node1's L1

        # node0 recomputes first (its L1 + the shared L2 entry were both
        # invalidated by the version bump / pub/sub event it just triggered).
        after0 = node0.execute(sql)
        assert after0.tier_hit == TierHit.MISS
        assert after0.rows[0][0] > before.rows[0][0]

        # node1 may now legitimately observe an L2 hit -- but only because
        # node0's recompute already refreshed the shared L2 entry to the new
        # table version. What must never happen is node1 serving the *old*
        # pre-write value, at any tier.
        after1 = node1.execute(sql)
        assert after1.rows == after0.rows
    finally:
        cluster.close()


def test_dynamic_rewrite_applied_end_to_end(config):
    cluster = EngineCluster(config, num_nodes=1)
    try:
        _seed(cluster)
        conn = cluster.backend.raw_connection()
        conn.execute("""
            CREATE TABLE agg_daily_region_revenue AS
            SELECT region_id, CAST(event_time AS DATE) AS event_day, sum(revenue) AS total_revenue
            FROM fact_order_events GROUP BY region_id, CAST(event_time AS DATE)
        """)
        node = cluster.route()
        sql = """SELECT region_id, date_trunc('day', event_time) AS d, sum(revenue) AS rev
                 FROM fact_order_events
                 GROUP BY region_id, date_trunc('day', event_time)"""
        result = node.execute(sql)
        assert "rollup_rewrite" in result.rewrites_applied
    finally:
        cluster.close()
