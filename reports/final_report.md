# Day 25 Reliability Report — Reliability Engineering for Production Agents

## 1. Architecture summary

The system implements a production-grade Reliability Layer for an LLM Agent Gateway. Incoming queries are routed through a multi-tier resilience architecture consisting of:
1. **Semantic Caching Layer (`ResponseCache` / `SharedRedisCache`)**: Intercepts requests using character 3-gram + word token Cosine Similarity, with privacy guardrails (`_is_uncacheable`) and false-hit detection (`_looks_like_false_hit` for year/number mismatches).
2. **Circuit Breaker Machine (`CircuitBreaker`)**: Protects downstream LLM providers against cascade failures using a 3-state machine (`CLOSED` -> `OPEN` -> `HALF_OPEN` -> `CLOSED`), preventing retry storms and failing fast when a provider degrades.
3. **Provider Fallback Chain (`ReliabilityGateway`)**: Iterates through prioritized LLM providers (`primary` -> `backup`), failing over automatically upon exceptions or open circuit states.
4. **Static Degraded Fallback**: Returns a predictable, graceful message (`"The service is temporarily degraded. Please try again soon."`) if all providers in the chain fail.

```
User Request
    |
    v
[Reliability Gateway]
    |
    +---> [Cache Check: Semantic / Redis] ---> HIT? ---> Return Cached Response (0ms, $0)
    |                                                |
    |                                                v MISS / Uncacheable
    +---> [Circuit Breaker: Primary Provider] ---------> Primary LLM (180ms, $0.01/1k tokens)
    |         | (OPEN / Provider Failure)
    |         v
    +---> [Circuit Breaker: Backup Provider] ----------> Backup LLM (260ms, $0.006/1k tokens)
    |         | (OPEN / Provider Failure)
    |         v
    +---> [Static Fallback Message] -------------------> "Service temporarily degraded"
```

---

## 2. Configuration

| Setting | Value | Reason / Justification |
|---|---:|---|
| `failure_threshold` | `3` | Tripping circuit after 3 consecutive failures prevents false alarms from single transient blips while rapidly isolating failing providers. |
| `reset_timeout_seconds` | `2.0 s` | Cooldown period before probing recovery via HALF_OPEN. Short enough to recover quickly, long enough to avoid overwhelming the upstream provider. |
| `success_threshold` | `1` | Number of successful probe requests in HALF_OPEN before returning to CLOSED state and resuming normal routing. |
| `cache TTL` | `300 s` | 5-minute time-to-live balances freshness of dynamic data with high cache hit rates and cost savings for recurrent queries. |
| `similarity_threshold` | `0.92` | Strict cosine threshold over n-grams ensures semantic fidelity; prevents incorrect responses on subtly different queries while matching close variants. |
| `load_test requests` | `100` | 100 requests per chaos scenario ensures statistically meaningful metric calculation (P50/P95/P99 latency, availability, hit rate). |

---

## 3. SLO definitions

| SLI | SLO Target | Actual Value | Met? |
|---|---|---:|:---:|
| Availability | >= 99.0% | **99.33%** | **YES** |
| Latency P95 | < 2500 ms | **314.23 ms** | **YES** |
| Fallback Success Rate | >= 95.0% | **97.37%** | **YES** |
| Cache Hit Rate | >= 10.0% | **61.33%** | **YES** |
| Recovery Time | < 5000 ms | **2285.51 ms** | **YES** |

---

## 4. Metrics Summary

Metrics captured from execution of `reports/metrics.json` over 300 total requests across all chaos scenarios:

| Metric | Value |
|---|---:|
| `total_requests` | `300` |
| `availability` | `0.9933` (99.33%) |
| `error_rate` | `0.0067` (0.67%) |
| `latency_p50_ms` | `281.34 ms` |
| `latency_p95_ms` | `314.23 ms` |
| `latency_p99_ms` | `321.95 ms` |
| `fallback_success_rate` | `0.9737` (97.37%) |
| `cache_hit_rate` | `0.6133` (61.33%) |
| `circuit_open_count` | `10` |
| `recovery_time_ms` | `2285.51 ms` |
| `estimated_cost` | `$0.047944` |
| `estimated_cost_saved` | `$0.184000` |

---

## 5. Cache Comparison

Comparative benchmark running 300 requests across all chaos scenarios with cache enabled vs disabled:

| Metric | Without Cache | With Cache | Delta / Improvement |
|---|---:|---:|---|
| `availability` | 94.33% | **99.33%** | **+5.00%** availability boost |
| `error_rate` | 5.67% | **0.67%** | **-88.2%** error reduction |
| `latency_p50_ms` | 279.09 ms | **281.34 ms** | +2.25 ms (cache evaluation overhead) |
| `latency_p95_ms` | 319.12 ms | **314.23 ms** | **-4.89 ms** P95 tail latency reduction |
| `estimated_cost` | $0.123410 | **$0.047944** | **-61.15%** cost saved ($0.075466) |
| `circuit_open_count`| 25 | **10** | **-60.0%** breaker trips (cache absorbs load) |
| `cache_hit_rate` | 0.0% | **61.33%** | +61.33% hit rate |

---

## 6. Redis Shared Cache

### Production Relevance
- **Why in-memory cache is insufficient for multi-instance deployments**: In modern containerized/Kubernetes architectures, horizontal scaling deploys multiple gateway replicas behind a load balancer. In-memory caches are isolated per pod, resulting in cold-cache stampedes, duplicated LLM API costs, inconsistent answers across instances, and lost cache states during pod restarts or autoscaling.
- **How `SharedRedisCache` solves this**: Centralizes cache entries in Redis using hash structures (`rl:cache:<query_hash>`), providing shared state across all replicas, atomic operations, automatic TTL expirations via `EXPIRE`, and instant cache reuse across instances.

### Evidence of Shared State
Verification demonstrating two distinct `SharedRedisCache` instances accessing the identical state on Redis:

```python
c1 = SharedRedisCache("redis://localhost:6379/0", ttl_seconds=300, similarity_threshold=0.85, prefix="rl:evidence:")
c2 = SharedRedisCache("redis://localhost:6379/0", ttl_seconds=300, similarity_threshold=0.85, prefix="rl:evidence:")

c1.set("What is reliability engineering?", "Reliability engineering is the discipline of ensuring system availability and resilience.")
cached, score = c2.get("What is reliability engineering?")
# Output:
# Cross-instance read: Reliability engineering is the discipline of ensuring system availability and resilience. Score: 1.0
```

### Redis CLI Key Inspection
```bash
$ docker compose exec redis redis-cli KEYS "rl:evidence:*"
1) "rl:evidence:a5c01076ad5e"

$ docker compose exec redis redis-cli HGETALL "rl:evidence:a5c01076ad5e"
1) "response"
2) "Reliability engineering is the discipline of ensuring system availability and resilience."
3) "query"
4) "What is reliability engineering?"
```

---

## 7. Chaos Scenarios

| Scenario | Expected Behavior | Observed Behavior | Pass/Fail |
|---|---|---|:---:|
| `primary_timeout_100` | Primary fails 100%. Primary circuit breaker trips after 3 failures. All non-cached requests gracefully fall back to Backup provider. | Primary breaker transitioned to OPEN; traffic rerouted to backup with 97.4% fallback success. Zero unhandled crashes. | **PASS** |
| `primary_flaky_50` | Primary fails 50% randomly. Breaker oscillates between CLOSED, OPEN, and HALF_OPEN. | Circuit opened and recovered periodically; average recovery time 2285.5 ms. System maintained high availability. | **PASS** |
| `all_healthy` | Both providers 100% healthy. Primary handles all traffic, circuit remains CLOSED. | 100% primary provider routing; 0 circuit trips; optimal latency and token cost efficiency. | **PASS** |

---

## 8. Failure Analysis

### Remaining Weakness: Cache Scanning Overhead at Scale
In the current `SharedRedisCache.get()` implementation, similarity lookup iterates over all keys under the prefix using `SCAN` and computes n-gram similarity in Python. While efficient for hundreds of cached entries, this $O(N)$ scanning approach incurs noticeable latency and network bandwidth overhead when the cache grows to tens of thousands of items.

### Mitigation & Fix
1. **Vector Indexing (RedisVL / RediSearch)**: Deploy RediSearch with HNSW / Flat vector indexing and embeddings (e.g., SentenceTransformers or fastembed) to perform $O(\log N)$ approximate nearest neighbor (ANN) vector search directly within the Redis engine.
2. **Two-Stage Lookup Pipeline**: Use an exact MD5 hash lookup first ($O(1)$), followed by MinHash LSH (Locality-Sensitive Hashing) or Redis vector search if exact lookup misses.
3. **Distributed Circuit Breaker State**: Synchronize circuit breaker state counters across instances in Redis using atomic `INCR` + sliding window Redis scripts (`EVALSHA`) to ensure collective fast-fail across the whole gateway cluster.

---

## 9. Next Steps & Recommended Improvements

1. **Redis-Backed Distributed Circuit Breakers**: Move `failure_count` and `opened_at` to Redis hashes with atomic TTLs so all gateway replicas share circuit state simultaneously.
2. **Adaptive Cost-Aware Dynamic Routing**: Implement real-time budget tracking; when hourly/daily token cost hits 80% threshold, dynamically prioritize smaller/cheaper models and raise cache tolerance.
3. **Graceful Cache Fallback (Multi-tier Caching)**: Implement L1 (In-Memory LRU) + L2 (Redis) hybrid caching with fallback to L1 if Redis becomes temporarily unreachable.
