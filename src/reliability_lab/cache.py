from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

import redis as redis_lib

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache skeleton.

    Uses cosine similarity over character n-grams + word tokens, with
    privacy guardrails and false-hit detection.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity."""
        if _is_uncacheable(query):
            return (None, 0.0)

        now = time.time()
        self._entries = [entry for entry in self._entries if (now - entry.created_at) <= self.ttl_seconds]
        if not self._entries:
            return (None, 0.0)

        best_entry: CacheEntry | None = None
        best_score = 0.0
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_entry = entry

        if best_entry is not None and best_score >= self.similarity_threshold:
            if _looks_like_false_hit(query, best_entry.key):
                self.false_hit_log.append({
                    "query": query,
                    "cached_key": best_entry.key,
                    "reason": "date_or_number_mismatch",
                    "score": best_score,
                })
                return (None, best_score)
            return (best_entry.value, best_score)

        return (None, best_score)

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in cache with privacy guardrail."""
        if _is_uncacheable(query):
            return
        self._entries.append(
            CacheEntry(
                key=query,
                value=value,
                created_at=time.time(),
                metadata=metadata or {},
            )
        )

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        words = re.findall(r"\w+", text.lower().strip())
        tokens: list[str] = list(words)
        for w in words:
            if len(w) >= 3:
                for i in range(len(w) - 2):
                    tokens.append(w[i : i + 3])
            else:
                tokens.append(w)
        return tokens

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Compute semantic similarity between two strings using cosine over word tokens + 3-grams."""
        if a == b:
            return 1.0
        tokens_a = ResponseCache._tokenize(a)
        tokens_b = ResponseCache._tokenize(b)
        if not tokens_a or not tokens_b:
            return 0.0
        vec_a = Counter(tokens_a)
        vec_b = Counter(tokens_b)
        dot = sum(vec_a[k] * vec_b[k] for k in vec_a if k in vec_b)
        norm_a = math.sqrt(sum(v * v for v in vec_a.values()))
        norm_b = math.sqrt(sum(v * v for v in vec_b.values()))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments."""

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except redis_lib.RedisError:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis."""
        if _is_uncacheable(query):
            return (None, 0.0)

        exact_key = f"{self.prefix}{self._query_hash(query)}"
        try:
            exact_resp = self._redis.hget(exact_key, "response")
            if exact_resp is not None:
                return (str(exact_resp), 1.0)
        except redis_lib.RedisError:
            pass

        best_response: str | None = None
        best_query: str | None = None
        best_score = 0.0

        try:
            for key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_data = self._redis.hgetall(key)
                if not cached_data or "query" not in cached_data or "response" not in cached_data:
                    continue
                cached_q = cached_data["query"]
                score = ResponseCache.similarity(query, cached_q)
                if score > best_score:
                    best_score = score
                    best_response = cached_data["response"]
                    best_query = cached_q
        except redis_lib.RedisError:
            return (None, 0.0)

        if best_response is not None and best_query is not None and best_score >= self.similarity_threshold:
            if _looks_like_false_hit(query, best_query):
                self.false_hit_log.append({
                    "query": query,
                    "cached_key": best_query,
                    "reason": "date_or_number_mismatch",
                    "score": best_score,
                })
                return (None, best_score)
            return (best_response, best_score)

        return (None, best_score)

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL."""
        if _is_uncacheable(query):
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        try:
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except redis_lib.RedisError:
            pass

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
