"""Generates the synthetic analytical dataset used for the benchmark:
an e-commerce style star schema with a 10M+ row fact table.

Usage:
    python scripts/generate_dataset.py [--rows 12000000] [--out data/warehouse.duckdb]

Everything is generated in-database with DuckDB's own vectorized SQL
(range() + random()/hashing), which is what makes generating 12M+ rows
practical on a modest 2-core/3GB sandbox in well under a minute -- a Python
row-by-row generator would take orders of magnitude longer.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb


def generate(db_path: str, fact_rows: int, num_customers: int = 500_000, num_products: int = 10_000,
             num_regions: int = 24, days_back: int = 730, seed: float = 42.0):
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass
    wal_path = db_path + ".wal"
    if os.path.exists(wal_path):
        try:
            os.remove(wal_path)
        except OSError:
            pass

    con = duckdb.connect(db_path)
    con.execute("PRAGMA memory_limit='2GB'")
    con.execute("PRAGMA threads=2")
    con.execute(f"SELECT setseed({seed / 2147483647.0})")

    t0 = time.time()

    con.execute(f"""
        CREATE TABLE dim_region AS
        SELECT
            i AS region_id,
            'Region-' || i AS region_name,
            ['NA','EMEA','APAC','LATAM'][1 + (i % 4)] AS macro_area
        FROM range({num_regions}) t(i)
    """)

    con.execute(f"""
        CREATE TABLE dim_customer AS
        SELECT
            i AS customer_id,
            (i % {num_regions}) AS region_id,
            2019 + CAST(random() * 7 AS INT) AS signup_year,
            ['bronze','silver','gold','platinum'][1 + CAST(random()*4 AS INT)] AS tier
        FROM range({num_customers}) t(i)
    """)

    con.execute(f"""
        CREATE TABLE dim_product AS
        SELECT
            i AS product_id,
            ['electronics','home','apparel','grocery','toys','sports','books','beauty'][1 + CAST(random()*8 AS INT)] AS category,
            round(5 + random() * 495, 2) AS list_price
        FROM range({num_products}) t(i)
    """)

    print(f"dimensions created in {time.time()-t0:.1f}s")
    t1 = time.time()

    # Skewed access pattern baked into the *data* itself (a small number of
    # hot customers/products/regions account for a disproportionate share of
    # rows), which is realistic for e-commerce traffic and is what makes the
    # query workload's repeat-vs-cold behavior meaningful later.
    con.execute(f"""
        CREATE TABLE fact_order_events AS
        SELECT
            i AS order_id,
            CAST(pow(random(), 2.5) * {num_customers} AS BIGINT) % {num_customers} AS customer_id,
            CAST(pow(random(), 2.0) * {num_products} AS BIGINT) % {num_products} AS product_id,
            CAST(random() * {num_regions} AS BIGINT) % {num_regions} AS region_id,
            TIMESTAMP '2026-07-05 00:00:00' - INTERVAL (CAST(random() * {days_back} AS BIGINT)) DAY
                - INTERVAL (CAST(random() * 86400 AS BIGINT)) SECOND AS event_time,
            CASE WHEN random() < 0.85 THEN 'purchase' WHEN random() < 0.95 THEN 'view' ELSE 'return' END AS event_type,
            1 + CAST(random() * 4 AS INT) AS quantity,
            round(5 + random() * 495, 2) AS revenue
        FROM range({fact_rows}) t(i)
        ORDER BY event_time
    """)
    print(f"fact table ({fact_rows:,} rows) created in {time.time()-t1:.1f}s")
    # Physically clustering the fact table by event_time is what lets DuckDB's
    # per-row-group zone maps (min/max) skip the vast majority of row groups
    # for the date-range filters that dominate this workload's queries --
    # the same principle behind time-partitioning in BigQuery/Redshift/Iceberg.

    t2 = time.time()
    con.execute("CREATE INDEX idx_fact_region ON fact_order_events(region_id)")
    con.execute("CREATE INDEX idx_fact_customer ON fact_order_events(customer_id)")
    con.execute("CREATE INDEX idx_fact_product ON fact_order_events(product_id)")
    print(f"indexes built in {time.time()-t2:.1f}s")

    t3 = time.time()
    con.execute("""
        CREATE TABLE agg_daily_region_revenue AS
        SELECT
            region_id,
            CAST(event_time AS DATE) AS event_day,
            sum(revenue) AS total_revenue,
            count(*) AS order_count
        FROM fact_order_events
        WHERE event_type = 'purchase'
        GROUP BY region_id, CAST(event_time AS DATE)
    """)
    print(f"daily rollup built in {time.time()-t3:.1f}s")

    counts = con.execute("""
        SELECT
            (SELECT count(*) FROM fact_order_events),
            (SELECT count(*) FROM dim_customer),
            (SELECT count(*) FROM dim_product),
            (SELECT count(*) FROM dim_region),
            (SELECT count(*) FROM agg_daily_region_revenue)
    """).fetchone()
    print(f"fact_order_events={counts[0]:,} dim_customer={counts[1]:,} dim_product={counts[2]:,} "
          f"dim_region={counts[3]:,} agg_daily_region_revenue={counts[4]:,}")
    print(f"total generation time: {time.time()-t0:.1f}s -> {db_path}")
    con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=12_000_000)
    parser.add_argument("--out", type=str, default="data/warehouse.duckdb")
    args = parser.parse_args()
    generate(args.out, args.rows)
