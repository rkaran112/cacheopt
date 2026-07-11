"""Central configuration for the optimizer.

All tunables live here so the cost model, cache tiers, and invalidation
protocol can be reasoned about (and re-tuned) in one place, the same way a
real production system exposes optimizer/cache knobs via config rather than
scattering magic numbers through the codebase.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # --- Storage (L3, persistent) ---
    duckdb_path: str = os.environ.get("CACHEOPT_DUCKDB_PATH", "data/warehouse.duckdb")
    duckdb_read_only: bool = False

    # --- L1: in-process memory buffer ---
    l1_max_entries: int = int(os.environ.get("CACHEOPT_L1_MAX_ENTRIES", "512"))
    l1_max_bytes: int = int(os.environ.get("CACHEOPT_L1_MAX_BYTES", str(64 * 1024 * 1024)))  # 64MB

    # --- L2: Redis (distributed, shared across nodes) ---
    redis_host: str = os.environ.get("REDIS_HOST", "localhost")
    redis_port: int = int(os.environ.get("REDIS_PORT", "6379"))
    redis_db: int = int(os.environ.get("REDIS_DB", "0"))
    # When true (default outside of explicit prod config), use an embedded
    # real Redis server (via redislite) so the whole system runs standalone
    # without requiring Docker. Set CACHEOPT_REDIS_MODE=external to talk to
    # docker-compose / a managed Redis instead.
    redis_mode: str = os.environ.get("CACHEOPT_REDIS_MODE", "embedded")
    redis_rdb_path: str = os.environ.get("CACHEOPT_REDIS_RDB", "data/cacheopt_redis.rdb")

    # --- Cache entry lifecycle ---
    default_ttl_seconds: int = int(os.environ.get("CACHEOPT_DEFAULT_TTL", "300"))

    # --- Access pattern analyzer ---
    hotness_half_life_seconds: float = 60.0  # recency decay half-life
    history_window: int = 200  # per-fingerprint execution history samples kept

    # --- Cost model ---
    # Calibrated tier latency floors (ms), overwritten at startup by
    # calibrate_tier_costs() with real measured numbers on this machine.
    cost_l1_ms: float = 0.01
    cost_l2_ms: float = 0.6
    cost_l3_min_ms: float = 5.0

    # Don't bother caching a result if recomputing it is cheaper than the
    # write+serialize+network cost of populating the cache (admission
    # control, same idea as TinyLFU admission in Caffeine/CDN caches).
    cache_admission_min_cost_ms: float = 1.0

    # Skip caching a result whose estimated row count exceeds this (protects
    # against caching a multi-million-row raw export). DuckDB's own planner
    # cardinality estimate for GROUP BY after a JOIN is often a poor proxy
    # for the true (much smaller, post-aggregation) output size, so this is
    # set well above typical dashboard-aggregate sizes to avoid false
    # positives while still catching genuinely large raw scans.
    max_cacheable_rows: int = 3_000_000


DEFAULT_CONFIG = Config()
