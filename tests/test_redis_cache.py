"""Tests for SharedRedisCache — requires Redis running on localhost:6379.

Start Redis with: docker compose up -d
"""
from __future__ import annotations

import time

import pytest

from reliability_lab.cache import SharedRedisCache


def _redis_available() -> bool:
    try:
        import redis as redis_lib

        r = redis_lib.Redis.from_url("redis://localhost:6379/0")
        r.ping()
        r.close()
        return True
    except redis_lib.RedisError:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_available(),
    reason="Redis not running — start with: docker compose up -d",
)


@pytest.fixture
def cache() -> SharedRedisCache:  # type: ignore[misc]
    c = SharedRedisCache(
        redis_url="redis://localhost:6379/0",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:test:",
    )
    c.flush()
    yield c  # type: ignore[misc]
    c.flush()
    c.close()


def test_redis_connection(cache: SharedRedisCache) -> None:
    """Verifies Redis connectivity — should pass before implementing get/set."""
    assert cache.ping()


def test_set_and_exact_get(cache: SharedRedisCache) -> None:
    cache.set("hello world", "response text")
    cached, score = cache.get("hello world")
    assert cached == "response text"
    assert score == 1.0


def test_ttl_expiry() -> None:
    c = SharedRedisCache(
        redis_url="redis://localhost:6379/0",
        ttl_seconds=1,
        similarity_threshold=0.5,
        prefix="rl:test:ttl:",
    )
    c.flush()
    c.set("temp query", "temp response")
    time.sleep(1.5)
    cached, _ = c.get("temp query")
    assert cached is None
    c.flush()
    c.close()


def test_shared_state_across_instances() -> None:
    """Two SharedRedisCache instances on same Redis should see same data."""
    c1 = SharedRedisCache(
        redis_url="redis://localhost:6379/0",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:test:shared:",
    )
    c2 = SharedRedisCache(
        redis_url="redis://localhost:6379/0",
        ttl_seconds=60,
        similarity_threshold=0.5,
        prefix="rl:test:shared:",
    )
    c1.flush()
    c1.set("shared query", "shared response")
    cached, _ = c2.get("shared query")
    assert cached == "shared response"
    c1.flush()
    c1.close()
    c2.close()


def test_privacy_query_not_cached(cache: SharedRedisCache) -> None:
    cache.set("account balance for user 123", "Balance: $500")
    cached, _ = cache.get("account balance for user 123")
    assert cached is None


def test_false_hit_different_years(cache: SharedRedisCache) -> None:
    cache.set("refund policy for 2024", "old policy")
    cached, _ = cache.get("refund policy for 2026")
    assert cached is None
    assert len(cache.false_hit_log) >= 1
