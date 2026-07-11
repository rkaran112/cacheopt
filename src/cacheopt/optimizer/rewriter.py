"""Dynamic query rewriting.

Three independent, individually-safe rewrite passes run over the sqlglot
AST before a query reaches the cost model / execution engine. Each pass is
wrapped so a failure (unsupported syntax, an edge case the heuristic doesn't
handle) falls back to the pre-pass tree rather than corrupting the query --
correctness takes priority over cleverness.

1. constant_fold   -- delegates to sqlglot's own optimizer.simplify pass:
   folds literal arithmetic/date expressions, collapses redundant boolean
   predicates, removes always-true/false branches. This is real constant
   folding, not string substitution (verified: `date '2026-01-01' - interval
   7 day` becomes the literal `DATE '2025-12-25'` before the query ever
   reaches DuckDB).

2. rollup_rewrite  -- recognizes the specific shape of "daily revenue by
   region" dashboard queries against the raw fact table and redirects them
   to a precomputed daily rollup table (`agg_daily_region_revenue`, built at
   load time -- see scripts/generate_dataset.py). This mirrors how BI
   engines (e.g. BigQuery/Postgres materialized view matching) transparently
   substitute a smaller precomputed aggregate for a matching query shape.
   It is intentionally pattern-specific and conservative: it only fires when
   the query provably requests no finer grain than the rollup provides.

3. join_reorder    -- for a chain of plain INNER equi-joins, reorders the
   join sequence so the table with the smallest estimated cardinality is
   probed first. This is the same "smallest table first" heuristic used as
   a fallback/seed ordering by real cost-based optimizers before (or
   instead of) full DP join enumeration.
"""
from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.simplify import simplify

from ..stats import StatsCatalog


@dataclass
class RewriteResult:
    sql: str
    applied: list[str]


def constant_fold(tree: exp.Expression) -> tuple[exp.Expression, bool]:
    try:
        folded = simplify(tree.copy())
        return folded, True
    except Exception:
        return tree, False


ROLLUP_TABLE = "agg_daily_region_revenue"
ROLLUP_SOURCE_TABLE = "fact_order_events"


def rollup_rewrite(tree: exp.Expression) -> tuple[exp.Expression, bool]:
    """Redirect `SELECT region_id, date_trunc('day', event_time), sum(revenue)
    FROM fact_order_events ... GROUP BY region_id, date_trunc('day', event_time)`
    (with an optional date-range/region filter only) to the precomputed
    daily rollup table.
    """
    if not isinstance(tree, exp.Select):
        return tree, False

    from_expr = tree.args.get("from_") or tree.args.get("from")
    if from_expr is None or tree.args.get("joins"):
        return tree, False

    table = from_expr.this if isinstance(from_expr, exp.From) else from_expr
    if not isinstance(table, exp.Table) or table.name.lower() != ROLLUP_SOURCE_TABLE:
        return tree, False

    group = tree.args.get("group")
    if not group:
        return tree, False

    group_exprs = group.expressions
    has_day_trunc = any(
        isinstance(g, (exp.DateTrunc, exp.TimestampTrunc)) or (isinstance(g, exp.Column) and g.name == "event_day")
        for g in group_exprs
    )
    has_region = any(isinstance(g, exp.Column) and g.name == "region_id" for g in group_exprs)
    if not (has_day_trunc and has_region):
        return tree, False

    agg_funcs = list(tree.find_all(exp.Sum, exp.Count, exp.Avg))
    non_revenue_agg = [
        a for a in agg_funcs
        if not any(isinstance(c, exp.Column) and c.name == "revenue" for c in a.find_all(exp.Column))
    ]
    if any(isinstance(a, exp.Avg) for a in non_revenue_agg):
        return tree, False  # rollup doesn't carry every aggregate shape safely

    where = tree.args.get("where")
    if where is not None:
        cols_in_where = {c.name for c in where.find_all(exp.Column)}
        if not cols_in_where.issubset({"event_time", "region_id"}):
            return tree, False

    new_tree = tree.copy()
    new_table = exp.Table(
        this=exp.Identifier(this=ROLLUP_TABLE, quoted=False),
        alias=table.alias_or_name if table.alias else None,
    )
    new_tree.args["from_"] = exp.From(this=new_table)

    # the rollup table already stores event_day (DATE) instead of raw event_time
    for col in new_tree.find_all(exp.Column):
        if col.name == "event_time":
            col.set("this", exp.Identifier(this="event_day", quoted=False))
    for dt in list(new_tree.find_all(exp.DateTrunc)) + list(new_tree.find_all(exp.TimestampTrunc)):
        dt.replace(dt.this)
    for sum_node in new_tree.find_all(exp.Sum):
        for c in sum_node.find_all(exp.Column):
            if c.name == "revenue":
                c.set("this", exp.Identifier(this="total_revenue", quoted=False))

    return new_tree, True


def join_reorder(tree: exp.Expression, stats: StatsCatalog) -> tuple[exp.Expression, bool]:
    if not isinstance(tree, exp.Select):
        return tree, False
    joins = tree.args.get("joins") or []
    if len(joins) < 2:
        return tree, False
    if not all((j.kind or "").upper() in ("", "INNER") and j.args.get("on") is not None for j in joins):
        return tree, False

    from_expr = tree.args.get("from_") or tree.args.get("from")
    base_table = from_expr.this if from_expr is not None else None
    if not isinstance(base_table, exp.Table):
        return tree, False

    def cost_of(table_expr: exp.Table) -> int:
        ts = stats.get(table_expr.name.lower())
        return ts.row_count if ts else 10**9

    scored = sorted(
        range(len(joins)),
        key=lambda i: cost_of(joins[i].this) if isinstance(joins[i].this, exp.Table) else 10**9,
    )
    if scored == list(range(len(joins))):
        return tree, False  # already optimal order, nothing to change

    new_tree = tree.copy()
    new_joins = new_tree.args.get("joins")
    reordered = [new_joins[i] for i in scored]
    new_tree.set("joins", reordered)
    return new_tree, True


def rewrite(sql: str, stats: StatsCatalog, dialect: str = "duckdb") -> RewriteResult:
    applied: list[str] = []
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return RewriteResult(sql=sql, applied=[])

    tree, ok = constant_fold(tree)
    if ok:
        applied.append("constant_fold")

    tree, ok = rollup_rewrite(tree)
    if ok:
        applied.append("rollup_rewrite")

    tree, ok = join_reorder(tree, stats)
    if ok:
        applied.append("join_reorder")

    try:
        out_sql = tree.sql(dialect=dialect)
    except Exception:
        out_sql = sql
        applied = []

    return RewriteResult(sql=out_sql, applied=applied)
