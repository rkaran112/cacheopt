"""Query templates used by the benchmark: a representative mix of the kinds
of queries a BI/analytics dashboard issues against an e-commerce star
schema (revenue rollups, top-N breakdowns, cohort analysis, joins across
fact + dimension tables).

Each template has a small pool of "hot" parameter combinations (the panels
a dashboard keeps re-querying/auto-refreshing) and can also be instantiated
with a fresh, never-repeated parameter combination to model one-off ad hoc
analyst queries. This hot/cold split is what makes "speedup on repeat
workloads" a meaningful, honestly-measured number rather than an artifact
of a workload that's either 100% repeats or 100% unique queries.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class Template:
    name: str
    sql_fn: callable
    hot_params: list[tuple]
    cold_param_fn: callable  # () -> a fresh, unique param tuple


def _rand_day_offset(rng):
    return rng.randint(1, 700)


TEMPLATES: list[Template] = [
    Template(
        name="revenue_by_region_last_n_days",
        sql_fn=lambda days: f"""
            SELECT region_id, sum(revenue) AS total_revenue, count(*) AS n
            FROM fact_order_events
            WHERE event_time >= TIMESTAMP '2026-07-05' - INTERVAL {days} DAY
              AND event_type = 'purchase'
            GROUP BY region_id
            ORDER BY total_revenue DESC
        """,
        hot_params=[(7,), (14,), (30,), (90,)],
        cold_param_fn=lambda rng: (rng.randint(1, 365),),
    ),
    Template(
        name="daily_region_rollup",
        sql_fn=lambda region_id, days: f"""
            SELECT region_id, date_trunc('day', event_time) AS d, sum(revenue) AS rev
            FROM fact_order_events
            WHERE event_time >= TIMESTAMP '2026-07-05' - INTERVAL {days} DAY
              AND region_id = {region_id}
            GROUP BY region_id, date_trunc('day', event_time)
        """,
        hot_params=[(0, 30), (1, 30), (2, 30), (3, 7), (0, 7)],
        cold_param_fn=lambda rng: (rng.randint(0, 23), rng.randint(1, 365)),
    ),
    Template(
        name="top_products_by_revenue",
        sql_fn=lambda days, limit: f"""
            SELECT p.product_id, p.category, sum(f.revenue) AS total_revenue
            FROM fact_order_events f
            JOIN dim_product p ON f.product_id = p.product_id
            WHERE f.event_time >= TIMESTAMP '2026-07-05' - INTERVAL {days} DAY
            GROUP BY p.product_id, p.category
            ORDER BY total_revenue DESC
            LIMIT {limit}
        """,
        hot_params=[(7, 10), (30, 10), (30, 20)],
        cold_param_fn=lambda rng: (rng.randint(1, 180), rng.choice([5, 10, 15, 25])),
    ),
    Template(
        name="customer_lifetime_value",
        sql_fn=lambda customer_id: f"""
            SELECT customer_id, sum(revenue) AS ltv, count(*) AS orders
            FROM fact_order_events
            WHERE customer_id = {customer_id}
            GROUP BY customer_id
        """,
        hot_params=[(101,), (5502,), (98765,), (250000,)],
        cold_param_fn=lambda rng: (rng.randint(0, 499_999),),
    ),
    Template(
        name="category_revenue_breakdown",
        sql_fn=lambda days: f"""
            SELECT p.category, sum(f.revenue) AS total_revenue, count(*) AS n
            FROM fact_order_events f
            JOIN dim_product p ON f.product_id = p.product_id
            WHERE f.event_time >= TIMESTAMP '2026-07-05' - INTERVAL {days} DAY
            GROUP BY p.category
            ORDER BY total_revenue DESC
        """,
        hot_params=[(30,), (7,), (90,)],
        cold_param_fn=lambda rng: (rng.randint(1, 300),),
    ),
    Template(
        name="region_macro_area_breakdown",
        sql_fn=lambda days: f"""
            SELECT r.macro_area, sum(f.revenue) AS total_revenue
            FROM fact_order_events f
            JOIN dim_region r ON f.region_id = r.region_id
            WHERE f.event_time >= TIMESTAMP '2026-07-05' - INTERVAL {days} DAY
            GROUP BY r.macro_area
        """,
        hot_params=[(7,), (30,)],
        cold_param_fn=lambda rng: (rng.randint(1, 400),),
    ),
    Template(
        name="signup_cohort_revenue",
        sql_fn=lambda signup_year: f"""
            SELECT c.signup_year, c.tier, sum(f.revenue) AS total_revenue
            FROM fact_order_events f
            JOIN dim_customer c ON f.customer_id = c.customer_id
            WHERE c.signup_year = {signup_year}
            GROUP BY c.signup_year, c.tier
        """,
        hot_params=[(2023,), (2024,), (2025,)],
        cold_param_fn=lambda rng: (rng.randint(2019, 2025),),
    ),
    Template(
        name="event_type_breakdown",
        sql_fn=lambda days: f"""
            SELECT event_type, count(*) AS n, sum(revenue) AS total_revenue
            FROM fact_order_events
            WHERE event_time >= TIMESTAMP '2026-07-05' - INTERVAL {days} DAY
            GROUP BY event_type
        """,
        hot_params=[(1,), (7,), (30,)],
        cold_param_fn=lambda rng: (rng.randint(1, 500),),
    ),
    Template(
        name="single_region_point_lookup",
        sql_fn=lambda region_id: f"""
            SELECT count(*) AS n FROM fact_order_events WHERE region_id = {region_id}
        """,
        hot_params=[(0,), (1,), (2,)],
        cold_param_fn=lambda rng: (rng.randint(0, 23),),
    ),
]


def generate_workload(n: int, p_hot: float = 0.75, seed: int = 7) -> list[tuple[str, str]]:
    """Returns a list of (template_name, rendered_sql) pairs simulating
    realistic dashboard + ad hoc traffic. Hot picks reuse one of a
    template's fixed parameter combos (so the exact SQL text repeats
    across the workload -- genuine cache_key repeats). Cold picks generate
    a fresh, essentially never-repeated parameter combo."""
    rng = random.Random(seed)
    by_name = {t.name: t for t in TEMPLATES}
    workload = []
    for _ in range(n):
        template = rng.choice(TEMPLATES)
        if rng.random() < p_hot:
            params = rng.choice(template.hot_params)
        else:
            params = template.cold_param_fn(rng)
        sql = template.sql_fn(*params)
        workload.append((template.name, sql))
    return workload
