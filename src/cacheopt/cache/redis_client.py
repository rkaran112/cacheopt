"""Redis connection factory.

Two modes, selected by Config.redis_mode:

* "external" -- standard redis-py TCP client pointed at REDIS_HOST/REDIS_PORT.
  This is the production path: point it at docker-compose's Redis, or at a
  managed cluster (ElastiCache/Memorystore/Redis Enterprise). No code above
  this module knows or cares that it's talking to a "real" server.

* "embedded" -- uses `redislite`, which launches an actual redis-server
  binary as a child process and exposes the same redis-py client interface
  over a unix socket. This lets the whole project run standalone (tests, CI,
  sandboxes without Docker) against a genuine Redis server rather than a
  Python re-implementation, while keeping the exact same client API as
  production.

Either way, every other module in this codebase only ever sees a standard
`redis.Redis`-compatible client.
"""
from __future__ import annotations

import threading

from ..config import Config

_lock = threading.Lock()
_singleton = None


def get_redis_client(config: Config):
    global _singleton
    with _lock:
        if _singleton is not None:
            return _singleton
        if config.redis_mode == "external":
            import redis
            _singleton = redis.Redis(host=config.redis_host, port=config.redis_port, db=config.redis_db)
        else:
            from redislite import Redis as RedisLite
            _singleton = RedisLite(config.redis_rdb_path)
        return _singleton


def reset_client():
    """Test helper: drop the cached singleton (e.g. between test modules)."""
    global _singleton
    with _lock:
        if _singleton is not None:
            try:
                _singleton.shutdown()
            except Exception:
                pass
        _singleton = None
