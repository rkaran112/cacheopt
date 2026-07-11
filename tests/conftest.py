import dataclasses
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cacheopt.cache import redis_client as redis_client_module
from cacheopt.config import Config


@pytest.fixture
def config(tmp_path):
    redis_client_module.reset_client()
    cfg = dataclasses.replace(
        Config(),
        duckdb_path=str(tmp_path / "test.duckdb"),
        redis_rdb_path=str(tmp_path / "test_redis.rdb"),
        redis_mode=os.environ.get("CACHEOPT_REDIS_MODE", "embedded"),
        cache_admission_min_cost_ms=0.0,  # cache everything in tests, deterministic
        default_ttl_seconds=300,
    )
    yield cfg
    redis_client_module.reset_client()
