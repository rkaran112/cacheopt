# How cacheopt works (plain-language walkthrough)

This document explains the system end to end, in plain language, for
anyone reading the code who isn't already deep in database internals. It
also states, honestly, what's a real production-grade technique versus
what's a deliberately simplified version of one.

## The problem this solves

Analytical dashboards ask the same handful of questions over and over:
"what was revenue by region this week," "who are the top customers this
month," refreshed every few minutes by every person looking at the
dashboard. Underneath, those questions run against a database holding
millions or billions of rows. Recomputing the same answer from scratch
every single time is wasteful — the data usually hasn't changed since the
last time someone asked.

The fix, in principle, is simple: remember the answer to a question you've
already answered, and only recompute it when the underlying data actually
changes. The hard parts are: *where* do you remember it (memory is fast but
small and local to one server; a shared cache is bigger but slower;
sometimes it's not worth remembering at all), *how* do you recognize "the
same question" when it's phrased slightly differently, and *how* do you
guarantee you never hand someone a stale answer after the data changes.
This project is a working answer to all three.

## The three places an answer can live

Think of it like a person answering questions at a desk:

1. **L1 — sticky notes on the desk (in-process memory).** Instant to check,
   but only visible to this one desk (one running copy of the application).
   Small (a bounded number of entries / bytes), so only the most-asked
   questions get a sticky note. Implementation: `cache/memory_buffer.py`,
   a straightforward size-aware LRU (least-recently-used entries get thrown
   away first when the desk runs out of room).

2. **L2 — a shared filing cabinet down the hall (Redis).** A bit slower to
   walk to than a sticky note, but every desk in the building (every
   running copy of the application, i.e. every "node") shares the same
   cabinet. If one node already computed an answer, every other node
   benefits immediately. Implementation: `cache/redis_cache.py`, using the
   real Redis wire protocol via `redis-py` — in this repo it's pointed at
   either a real Redis container (`docker-compose.yml`) or an embedded real
   `redis-server` binary for zero-setup local runs (`redislite`); the
   application code can't tell the difference.

3. **L3 — going to the archive and looking it up yourself (DuckDB).** The
   source of truth. Always correct, always available, but the slowest
   option because it means actually scanning the data. Implementation:
   `storage/duckdb_backend.py`. DuckDB is a genuine embedded analytical
   (columnar, vectorized) database engine, not a stand-in.

Every query the system receives either gets answered from a sticky note,
from the filing cabinet, or by doing the real work — and it always tries
the cheapest option first.

## Recognizing "the same question" — query fingerprinting

Two SQL strings can be the same question asked two different ways:
`SELECT * FROM orders WHERE region='US'` and
`select  *  from orders   where region = 'US'` are identical in meaning.
`region='US'` and `region='EU'` are the *same shape* of question with a
different answer.

`fingerprint.py` parses every incoming query into a proper syntax tree
(using the `sqlglot` library, the same class of tool real databases use
internally) and produces two identifiers from it:

- a **cache key**: the exact question, byte-for-byte meaning, regardless of
  spacing/capitalization — this is what's used to look up a cached answer.
- a **template ID**: the *shape* of the question with the specific values
  (region='US' vs 'EU') stripped out — this is what's used to notice "this
  kind of question gets asked a lot," even though the specific values
  change every time (e.g. a dashboard filtering by "the last 7 days" — the
  date changes daily, but it's the same question shape). This mirrors what
  Postgres calls a `queryid` and Oracle calls a `SQL_ID`.

## Deciding whether it's worth remembering — the cost model

Not every answer is worth writing down. If a question is trivially fast to
re-answer from scratch, caching it just adds overhead (write the sticky
note, later notice it's out of date, throw it away) for no benefit. The
cost model (`optimizer/cost_model.py`) makes this decision per query:

1. **How expensive is this to compute from scratch?** It asks DuckDB's own
   query planner for its cardinality estimate (roughly, "how many rows will
   this touch") and multiplies by this machine's measured scan throughput
   (measured once at startup — see `CardinalityEstimator.calibrate`, which
   deliberately avoids `SELECT count(*)`, because DuckDB can answer that
   from metadata alone without reading any data, which would make the
   calibration wildly over-optimistic about real query cost).

2. **How likely is this exact question to be asked again soon?** This
   comes from the access-pattern tracker (`stats.py`), which keeps a
   frequency + recency score per template ID — the same idea used by
   adaptive cache-replacement algorithms like ARC and W-TinyLFU, simplified
   for this project: `hotness = log(1 + times_seen) * exp(-time_since_last_seen / half_life)`.

3. **Is the answer itself too big to be worth remembering?** A query that
   returns a handful of summary rows is cheap to cache. A query that
   returns millions of raw rows isn't a "cache-shaped" problem — better to
   just answer it directly and not clutter the cache tiers.

Combining these: a cheap, rarely-repeated question skips caching entirely.
An expensive, frequently-repeated question gets written to *both* the
sticky note (L1) and the filing cabinet (L2). An expensive but rarely
(so-far) repeated question goes to the filing cabinet only, since it's
shared and cheap to populate, but doesn't yet deserve a spot on every
node's limited desk space.

**Honest caveat:** DuckDB's cardinality estimate for a `GROUP BY` that
follows a `JOIN` is often not very accurate — it estimates the size of the
join's output, not the (usually much smaller) size after grouping. During
benchmarking, this showed up concretely: a query that groups fact-table
rows by region into 4 summary rows was estimated by DuckDB's planner at
over 2 million rows, purely because that's roughly the join's output size
before grouping. This project's size-based cache-admission check is
therefore intentionally set well above typical dashboard-aggregate sizes,
to avoid rejecting queries that are actually small, well-behaved caching
candidates. A production system with more time invested would build a
better post-aggregation cardinality model (e.g. using column
distinct-value counts, which `stats.py` already collects via
`approx_count_distinct` but doesn't yet feed into this specific estimate).

## Making the question itself cheaper — dynamic query rewriting

Before the cost model even runs, every query passes through
`optimizer/rewriter.py`, which tries three independent, safety-checked
rewrites (each falls back to the original query if anything looks
unexpected, so a rewrite bug can never produce a wrong answer, only a
slower one):

1. **Constant folding.** `WHERE event_time >= date '2026-01-01' - interval
   7 day` becomes `WHERE event_time >= DATE '2025-12-25'` before the query
   ever reaches the database — the arithmetic is done once, at rewrite
   time, using `sqlglot`'s own optimizer pass, not string manipulation.

2. **Materialized-rollup redirection.** Some dashboard questions ("daily
   revenue by region") are common enough that this project precomputes the
   answer once at data-load time into a small summary table
   (`agg_daily_region_revenue`, about 17,500 rows versus the 12 million-row
   source table). When the rewriter recognizes a query that asks exactly
   that question (and only that question — the check is deliberately
   conservative), it transparently redirects it to the small table instead.
   This is the same idea as materialized-view matching in BigQuery or
   Postgres, scoped down to one well-understood, provably-safe pattern
   rather than a general-purpose rewrite engine.

3. **Join reordering.** For a query joining several tables, the rewriter
   reorders the joins so the smallest table (by row count, from
   `stats.py`) is matched first — the same "smallest table first" heuristic
   real cost-based optimizers use as a starting point before more
   expensive join-order search.

## Keeping the cache correct — invalidation & consistency

This is the part that's easy to get wrong: once you start remembering
answers, you have to guarantee you throw them away the instant they stop
being true.

The design (`invalidation.py`) uses two mechanisms, and it's worth being
precise about what each one actually guarantees, rather than making a
vague "it's consistent" claim:

- **A version number per table, stored in Redis.** Every time a table is
  written to, its version number is atomically incremented (`INCR`, which
  Redis guarantees is atomic even under concurrent access). Every cached
  answer, at every tier, remembers which version of each table it was
  computed from.

- **On every L2 (Redis) read**, before trusting a cached answer, the system
  re-checks the current version number for the tables that answer depends
  on. If the version has moved on since the answer was cached, it's
  discarded and treated as a miss. Because the version counters and the
  cached answers live in the same Redis instance, **this makes L2 strongly
  consistent**: it is never possible to read a cached answer that's older
  than the last completed write, no matter how long the answer has been
  sitting in the cache or how long ago its TTL was set.

- **L1 (the per-node sticky notes) is not re-checked against Redis on
  every read** — doing so would defeat the entire point of having an
  instant, no-network local tier. Instead, whenever a write happens, the
  writer publishes an event over Redis pub/sub, and every node is
  listening in a background thread; the instant that event arrives
  (typically well under a millisecond in practice), the node throws away
  any sticky notes that depended on the table that changed.

**What this actually guarantees:** L2 never serves stale data. L1 can, in
principle, serve a very briefly stale answer during the gap between a
write happening and the pub/sub message being processed — a window
measured in milliseconds, not something a distributed consensus protocol
would be needed to close. This is a deliberate, honestly-stated trade-off:
a real "no staleness ever, anywhere, guaranteed" system would need
something like synchronous replication or a consensus protocol (e.g. Raft)
for the local caches too, which is a much bigger (and, for a
dashboard-latency cache, generally unnecessary) undertaking. `tests/test_invalidation.py`
verifies both halves of this claim directly: that a version bump makes an
L2 read miss immediately, and that a pub/sub event actually reaches and
clears every node's L1 within the test's wait window.

## Tying it together — the distributed execution planner

`optimizer/planner.py` is the one piece of code every query passes
through, in order:

1. Fingerprint the query (recognize what's being asked).
2. Rewrite the query (make it cheaper if possible).
3. Ask the cost model where to look / whether to cache the result.
4. Check L1, then L2, in that order, per the cost model's plan.
5. On a miss, actually run the query against DuckDB.
6. Write the result back into whichever tier(s) the cost model chose.
7. Record how long it all took, feeding back into the access-pattern
   tracker for next time.

"Distributed" here means multiple copies of this whole pipeline
(`QueryEngine` instances, see `engine.py`) run at once, each with its own
private L1 but all sharing the same L2 (Redis) and L3 (DuckDB). A cache
warm-up on one node is immediately visible to every other node through the
shared Redis layer — this is the actual distributed-caching behavior
exercised by the benchmark's 3-node `EngineCluster`, and it's a realistic
model of how a fleet of stateless application servers would front a shared
analytical database in production.

## Benchmark methodology

`scripts/generate_dataset.py` builds a synthetic e-commerce analytics
warehouse: a 12-million-row fact table of order events, four dimension
tables (customers, products, regions, and a precomputed daily rollup), with
realistic skew (a small number of customers/products account for a
disproportionate share of activity, the same as in real e-commerce data).
The fact table is physically sorted by event time at load time, which lets
DuckDB's built-in per-block min/max statistics ("zone maps") skip most of
the table for the date-range filters that dominate this workload — the
same principle behind time-partitioning in BigQuery, Redshift, and Iceberg.

`scripts/query_templates.py` defines 9 query shapes modeled on a real BI
dashboard: revenue-by-region rollups, top-product breakdowns, customer
lifetime value, cohort analysis, category and event-type breakdowns. Each
template has a small pool of "hot" parameter combinations (the handful of
panels a dashboard keeps re-querying) and can also generate a fresh,
never-repeated parameter combination to model one-off analyst queries. The
benchmark workload draws 88% of its queries from the hot pool and 12% cold
— modeling a realistic BI traffic mix where a small number of dashboards
account for most of the load, and a minority of traffic is genuinely
ad hoc. This is what makes "70.9% latency reduction for repeat queries" a
meaningful, specific claim rather than an artifact of a workload that's
either 100% repeats (trivially favors caching) or 100% unique queries
(caching can't help at all by construction).

`scripts/run_benchmark.py` replays the *identical* 1,000-query sequence
twice: once against DuckDB directly (no caching, no rewriting — "how it
looked before"), and once through the full 3-node engine cluster
(fingerprinting, rewriting, cost-based routing, tiered caching, and
invalidation, all live). Same queries, same order, same random seed, so
the comparison is apples-to-apples. `scripts/make_chart.py` renders the
results into `benchmarks/results/benchmark_chart.png`.

## Why the median (p50) barely moves but the tail (p95/p99) improves a lot

This is a real, expected pattern, not a modeling artifact. The optimizer
adds a small, roughly fixed cost to *every* query — parsing it, computing
its fingerprint, checking the cache — on the order of a few milliseconds.
For queries that were already fast (the median case, often simple
aggregations DuckDB answers quickly on its own), that fixed overhead is
a larger fraction of the total time, so the median can look flat or even
slightly worse. But the *expensive* queries — the ones sitting in the p95
and p99 tail, some taking over 200ms at baseline — are exactly the ones
that benefit most from being served out of a cache instead of recomputed,
which is why the tail improves dramatically (a 126ms p95 baseline drops to
31ms optimized; a 213ms p99 drops to 90ms). This is the textbook shape of
what caching is supposed to do: it doesn't make every query instant, it
protects you from your worst-case queries dominating the user experience.

## What a production hardening pass would add

This project is built to be genuinely functional and independently
verifiable (25 tests, a reproducible benchmark, real Redis and DuckDB), but
a few things are explicitly out of scope and worth naming rather than
glossing over:

- **Better cardinality estimation** for post-join aggregates (see the
  caveat above), likely using the column distinct-value counts already
  collected in `stats.py`.
- **Concurrent write handling at the storage layer.** `DuckDBBackend`
  serializes all access through a single lock, which is fine for a
  read-mostly analytical workload (the scenario this project targets) but
  would need a real concurrent-write story (e.g. a proper OLTP store
  feeding the warehouse, or DuckDB's newer concurrency features) for a
  write-heavy system.
- **A distributed consensus layer for L1**, if "zero staleness, anywhere,
  ever, provably" became a hard requirement rather than "stale for at most
  a few milliseconds after a write" being acceptable.
- **Adaptive TTL and eviction tuning based on production traffic**, rather
  than the fixed half-life and threshold constants in `config.py`, which
  were chosen to be reasonable defaults and validated against this
  project's specific benchmark traffic, not tuned against real production
  telemetry.
