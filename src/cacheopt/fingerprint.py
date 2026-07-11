"""Query fingerprinting.

Two related identifiers are computed for every incoming SQL string, the same
split used in production systems like Postgres' pg_stat_statements (queryid)
and Oracle's SQL_ID:

* `template_id`  -- identifies the *shape* of a query with literal values
  stripped out. "WHERE region = 'US'" and "WHERE region = 'EU'" collapse to
  the same template. This is what the access-pattern analyzer keys its
  frequency/recency statistics on, because a dashboard re-issuing the same
  query shape with a rolling date filter should be recognized as "the same
  query" for hotness scoring.

* `cache_key` -- identifies a query *and* its exact parameter values. This is
  the literal cache lookup key, since two calls to the same template with
  different literals generally produce different result sets.

Normalization is done via sqlglot's AST rather than string munging so that
whitespace, comments, keyword casing, and cosmetic differences never cause a
cache/stat miss for what is semantically the same query.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

import sqlglot
from sqlglot import exp


@dataclass(frozen=True)
class Fingerprint:
    template_id: str
    cache_key: str
    template_sql: str
    tables: tuple[str, ...]


def _extract_tables(tree: exp.Expression) -> tuple[str, ...]:
    names = sorted({t.name.lower() for t in tree.find_all(exp.Table) if t.name})
    return tuple(names)


def _literal_placeholder(lit: exp.Literal) -> exp.Expression:
    return exp.Placeholder(this="lit")


def fingerprint(sql: str, dialect: str = "duckdb") -> Fingerprint:
    """Parse `sql` and compute its template_id / cache_key fingerprints.

    Falls back to a raw-string hash if the query fails to parse (so the
    engine degrades gracefully on dialect features sqlglot doesn't cover,
    rather than crashing the whole request path).
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        digest = hashlib.sha256(sql.strip().lower().encode()).hexdigest()
        return Fingerprint(template_id=digest, cache_key=digest, template_sql=sql, tables=())

    tables = _extract_tables(tree)

    # cache_key: exact semantic form (literals kept), whitespace/case
    # normalized by round-tripping through sqlglot's canonical SQL generator.
    exact_sql = tree.sql(dialect=dialect, normalize=True)
    cache_key = hashlib.sha256(exact_sql.encode()).hexdigest()

    # template_id: same tree with every literal replaced by a placeholder.
    template_tree = tree.copy().transform(
        lambda node: _literal_placeholder(node) if isinstance(node, exp.Literal) else node
    )
    template_sql = template_tree.sql(dialect=dialect, normalize=True)
    template_id = hashlib.sha256(template_sql.encode()).hexdigest()

    return Fingerprint(
        template_id=template_id,
        cache_key=cache_key,
        template_sql=template_sql,
        tables=tables,
    )
