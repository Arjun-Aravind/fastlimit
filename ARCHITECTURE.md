# FastLimit Architecture - Deep Dive

This document explains the internal architecture of FastLimit, covering every major design decision, implementation detail, and the reasoning behind them. Everything is explained from first principles.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Why Redis?](#why-redis)
3. [Why Lua Scripts?](#why-lua-scripts)
4. [Integer Math Strategy](#integer-math-strategy)
5. [Key Naming Architecture](#key-naming-architecture)
6. [Backend Design](#backend-design)
7. [Multi-Tenant Architecture](#multi-tenant-architecture)
8. [Async Python Design](#async-python-design)
9. [Error Handling Philosophy](#error-handling-philosophy)
10. [Testing Strategy](#testing-strategy)
11. [Metrics & Observability](#metrics--observability)
12. [Performance Optimizations](#performance-optimizations)
13. [Security Considerations](#security-considerations)
14. [Design Trade-offs](#design-trade-offs)

---

## System Overview

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Application                           │
│                   (FastAPI, Django, etc.)                    │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                      RateLimiter                             │
│  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ Decorators  │  │ Middleware   │  │  Direct API  │       │
│  └─────────────┘  └──────────────┘  └──────────────┘       │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                   Algorithm Layer                            │
│  ┌──────────────┐ ┌─────────────┐ ┌─────────────────┐      │
│  │ Fixed Window │ │Token Bucket │ │ Sliding Window  │      │
│  └──────────────┘ └─────────────┘ └─────────────────┘      │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                    Redis Backend                             │
│  ┌──────────────────────────────────────────────┐           │
│  │         Lua Script Execution Engine          │           │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────┐ │           │
│  │  │ Fixed    │ │  Token   │ │   Sliding    │ │           │
│  │  │ Window   │ │  Bucket  │ │   Window     │ │           │
│  │  │ Script   │ │  Script  │ │   Script     │ │           │
│  │  └──────────┘ └──────────┘ └──────────────┘ │           │
│  └──────────────────────────────────────────────┘           │
└──────────────────────┬──────────────────────────────────────┘
                       │
                       ↓
┌─────────────────────────────────────────────────────────────┐
│                      Redis Server                            │
│         (In-Memory Data Store + Lua Runtime)                │
└─────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

1. **Application Layer**: Your application (FastAPI, Django, etc.)
   - Receives HTTP requests
   - Calls rate limiter before processing
   - Handles rate limit responses

2. **RateLimiter**: Main interface
   - Routes to appropriate algorithm
   - Parses rate limit strings ("100/minute")
   - Manages connection lifecycle
   - Provides decorator and middleware

3. **Algorithm Layer**: Algorithm implementations
   - Encapsulates algorithm-specific logic
   - Calculates parameters (refill rates, weights, etc.)
   - Delegates to Redis backend

4. **Redis Backend**: Low-level Redis operations
   - Executes Lua scripts atomically
   - Manages script caching (EVALSHA)
   - Handles connection pooling
   - Provides fallback mechanisms

5. **Redis Server**: Data storage
   - Stores counters, buckets, timestamps
   - Executes Lua scripts atomically
   - Manages TTLs and expiration

---

## Why Redis?

### Decision: Use Redis as the Backend

**Question**: Why not use PostgreSQL, MySQL, MongoDB, or in-memory Python dict?

**Answer**: Redis is the **only** practical choice for distributed rate limiting. Here's why:

### Requirement 1: Atomic Operations

**Problem**: Rate limiting requires atomic read-modify-write operations.

```python
# THIS IS BROKEN - Race condition!
current_count = await db.get_count(key)
if current_count < limit:
    await db.increment(key)  # ← Another request could execute here!
    return True
```

**Race Condition Example**:
```
Time    Thread A                Thread B
----    --------                --------
t0      Read count: 99
t1                              Read count: 99
t2      Check: 99 < 100 
t3                              Check: 99 < 100 
t4      Increment to 100
t5                              Increment to 101 ← LIMIT EXCEEDED!
```

**Why Redis?**
- **Lua Scripts**: All operations in a script are atomic
- **Single-threaded**: No race conditions
- **INCR/INCRBY**: Atomic increment operations

**PostgreSQL**:
- - Requires transactions (overhead)
- - Row-level locks (slower)
- - Connection overhead (not designed for high-frequency operations)
- - Can work, but much slower

**MongoDB**:
- - Eventual consistency issues
- - No native atomic increment + check
- - Higher latency

**In-Memory Dict**:
- - Not distributed (each worker has separate state)
- - Loses data on restart
- - No persistence

### Requirement 2: Performance

Rate limiting is on the **hot path** of every request. It must be **fast**.

```
Latency Requirements:
- Target: < 5ms p99
- Acceptable: < 10ms p99
- Unacceptable: > 50ms p99

Throughput Requirements:
- Target: > 10,000 checks/second per instance
- Acceptable: > 5,000 checks/second
- Unacceptable: < 1,000 checks/second
```

**Redis Performance**:
- - In-memory (microsecond access)
- - Single-threaded (no lock contention)
- - Optimized for small operations
- - 15,000+ checks/second easily

**PostgreSQL Performance**:
- - Disk-based (millisecond access even with cache)
- - Connection overhead
- - ~500-1,000 checks/second typical

### Requirement 3: TTL (Automatic Expiration)

Rate limit data should **automatically expire**. We don't want to manage cleanup manually.

**Redis**:
- - Native TTL support (`EXPIRE`)
- - Automatic eviction
- - Memory-efficient

**PostgreSQL**:
- - Requires manual cleanup (cron jobs)
- - Vacuum overhead
- - Complex to manage

### Requirement 4: Scalability

Rate limiting should **scale horizontally** easily.

**Redis**:
- - Redis Cluster for horizontal scaling
- - Minimal state (just counters)
- - Easy replication

**PostgreSQL**:
- - Harder to scale writes
- - Replication lag issues

### Decision Matrix

| Feature | Redis | PostgreSQL | MongoDB | In-Memory |
|---------|-------|------------|---------|-----------|
| Atomic Operations | - Lua | - Transactions | - Limited | - Not distributed |
| Performance | - 15K+ req/s | - 500-1K req/s | - 2K req/s | - Fast (but local) |
| TTL Support | - Native | - Manual | - Limited | - Manual |
| Latency | - < 1ms | - 5-10ms | - 5-15ms | - < 0.1ms (local) |
| Horizontal Scaling | - Easy | - Complex | - Easy | - Not applicable |
| Data Persistence | - Optional | - Durable | - Durable | - Volatile |

**Conclusion**: Redis is the clear winner for rate limiting.

---

## Why Lua Scripts?

### Decision: Use Lua Scripts Instead of Multiple Redis Commands

**Question**: Why not just use Python code with multiple Redis commands?

**Answer**: **Atomicity** and **performance**.

### The Atomicity Problem

**Bad Approach** (Multiple Redis Commands):
```python
async def check_rate_limit(key, limit):
    # THIS HAS A RACE CONDITION!
    current = await redis.get(key)
    if current is None:
        current = 0

    current = int(current)

    if current < limit:
        await redis.incr(key)  # ← RACE CONDITION HERE
        await redis.expire(key, 60)
        return True
    else:
        return False
```

**Problem**: Between `GET` and `INCR`, another request can execute!

```
Time    Request A                  Request B
----    ---------                  ---------
t0      GET → returns 99
t1                                 GET → returns 99
t2      Check: 99 < 100 
t3                                 Check: 99 < 100 
t4      INCR → now 100
t5                                 INCR → now 101 ← EXCEEDED LIMIT!
```

### The Solution: Lua Scripts

**Lua Script** (Atomic Execution):
```lua
-- ALL of this executes atomically!
local key = KEYS[1]
local max_requests = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])

local current = redis.call('INCRBY', key, cost)

if current == cost then
    redis.call('EXPIRE', key, window_seconds)
end

local ttl = redis.call('TTL', key)
if ttl <= 0 then
    redis.call('EXPIRE', key, window_seconds)
end

if current <= max_requests then
    local remaining = max_requests - current
    local retry_after = 0
    local reset_at = ttl
    return {1, remaining, retry_after, reset_at}
else
    local remaining = 0
    local retry_after = ttl * 1000
    local reset_at = ttl
    return {0, remaining, retry_after, reset_at}
end
```

**Why This Works**:
1. **Single Execution Context**: Redis is single-threaded
2. **No Interleaving**: Other commands wait until script finishes
3. **Atomic Guarantee**: Either all commands execute or none (script failure)

### Performance Benefit

**Multiple Commands** (3 round trips):
```python
current = await redis.get(key)        # Round trip 1: 0.5ms
await redis.incr(key)                  # Round trip 2: 0.5ms
await redis.expire(key, 60)            # Round trip 3: 0.5ms
# Total: 1.5ms
```

**Lua Script** (1 round trip):
```python
result = await redis.evalsha(script_sha, 1, key, ...)  # Round trip 1: 0.8ms
# Total: 0.8ms
```

**Savings**: ~47% faster (0.8ms vs 1.5ms)

### Script Caching (EVALSHA)

**Problem**: Sending full Lua script every time is expensive.

**Solution**: Cache scripts on Redis server.

```python
# First time: Load script
script_sha = await redis.script_load(lua_script)
# Returns SHA1 hash: "a1b2c3d4e5f6..."

# Future calls: Use SHA instead of full script
result = await redis.evalsha(script_sha, 1, key, ...)
```

**Implementation**:
```python
async def _execute_fixed_window_script(self, key, max_requests, window_seconds, cost):
    try:
        # Try EVALSHA (fast path)
        return await self._redis.evalsha(
            self._script_shas["fixed_window"],
            1, key, str(max_requests), str(window_seconds), str(cost)
        )
    except redis.exceptions.NoScriptError:
        # Script not cached, use EVAL (slow path)
        logger.warning("Script not cached, falling back to EVAL")
        return await self._redis.eval(
            self._scripts["fixed_window"],
            1, key, str(max_requests), str(window_seconds), str(cost)
        )
```

**Benefits**:
- Reduces network bandwidth (~500 bytes → 40 bytes)
- Faster parsing on Redis side
- Scripts cached across all connections

---

## Integer Math Strategy

### Decision: Multiply All Values by 1000

**Question**: Why not use floating-point numbers directly?

**Answer**: Lua's floating-point handling is **inconsistent** across Redis versions.

### The Floating-Point Problem

**Problematic Code**:
```lua
-- This can behave differently on different Redis versions!
local refill_rate = 1.667  -- 100 tokens / 60 seconds
local time_elapsed = 10
local tokens_to_add = refill_rate * time_elapsed  -- Could be 16.67 or 16.669999...

if tokens_to_add >= 16.67 then  -- Might fail due to precision!
    -- Allow request
end
```

**Issues**:
1. **Precision Loss**: `1.667 * 10` might be `16.669999999` or `16.670000001`
2. **Rounding Differences**: Different Lua versions round differently
3. **Comparison Issues**: `16.67 == 16.67` might be false!

### The Integer Math Solution

**Strategy**: Multiply everything by 1000 to eliminate decimals.

```python
# User input:
rate = "100/minute"
cost = 1

# Internal representation (multiply by 1000):
max_requests = 100 * 1000      # 100,000
cost_with_multiplier = 1 * 1000  # 1,000

# Redis operations (all integers):
current = INCRBY key 1000  # Increment by 1000, not 1

# When displaying to user (divide by 1000):
remaining_display = remaining // 1000  # 99,000 → 99
```

**Lua Script** (All Integer Math):
```lua
-- All values are integers!
local max_requests = tonumber(ARGV[1])  -- 100,000 (not 100)
local cost = tonumber(ARGV[2])          -- 1,000 (not 1)

local current = redis.call('INCRBY', key, cost)  -- Adds 1,000

if current <= max_requests then  -- 1,000 <= 100,000 
    local remaining = max_requests - current  -- 99,000
    return {1, remaining, 0, 0}
end
```

### Token Bucket Example (Integer Math)

**User Input**:
```python
rate = "100/minute"  # 100 tokens over 60 seconds
```

**Internal Calculation**:
```python
max_tokens = 100 * 1000  # 100,000
window_seconds = 60
refill_rate = max_tokens / window_seconds  # 100,000 / 60 = 1,666.666...
```

**Lua Script** (Integer Refill):
```lua
local max_tokens = 100000
local refill_rate = 1666.666...  -- PROBLEM: Float!

-- Solution: Refill rate as integer per millisecond
local refill_rate_ms = max_tokens / (window_seconds * 1000)
-- 100,000 / 60,000 = 1.666... → Still has decimals!

-- Better: Keep as integer division
local time_elapsed = 10  -- seconds
local tokens_to_add = (max_tokens * time_elapsed) / window_seconds
-- (100,000 * 10) / 60 = 1,000,000 / 60 = 16,666 (integer!)
```

**Final Implementation**:
```lua
local time_elapsed = current_time - last_refill
local tokens_to_add = (max_tokens * time_elapsed) / window_seconds
-- This stays in integer domain as long as possible
```

### Why 1000 as the Multiplier?

**Considered Options**:

| Multiplier | Precision | Max Rate Limit | Notes |
|------------|-----------|----------------|-------|
| 10 | 0.1 | 214,748,364/s | Too low precision |
| 100 | 0.01 | 21,474,836/s | Still limiting |
| 1000 | 0.001 | 2,147,483/s | Good balance  |
| 10000 | 0.0001 | 214,748/s | Overkill |

**Reasoning**:
- **Precision**: 0.001 (1/1000) is enough for rate limiting
  - Can represent fractional costs: 0.5 requests → 500
  - Accurate refill rates: 1.667 tokens/sec → 1667/sec
- **Range**: Can handle rates up to 2.1M requests/second
  - Far exceeds practical limits (Redis max ~100K req/s)
- **Human-Readable**: Easy to divide by 1000 mentally
  - 99,000 → 99 requests
  - 1,500 → 1.5 requests

---

## Key Naming Architecture

### Decision: Algorithm-Specific Key Patterns

**Question**: Why not use the same key pattern for all algorithms?

**Answer**: Each algorithm has **different storage needs**.

### Fixed Window: Time-Based Keys

**Pattern**: `{prefix}:{identifier}:{tenant}:{window_timestamp}`

**Example**: `ratelimit:user123:premium:1700000100`

**Breakdown**:
- `ratelimit`: Namespace prefix (configurable)
- `user123`: User/resource identifier
- `premium`: Tenant type (for multi-tenancy)
- `1700000100`: Window start timestamp (Unix time / window_seconds)

**Why Time-Based?**
- Each window needs its own counter
- Counter resets when window changes
- TTL automatically cleans up old windows

**Window Calculation**:
```python
current_time = 1700000142  # 14:35:42
window_seconds = 60

# Calculate window start (round down to nearest minute)
window_start = current_time - (current_time % window_seconds)
# 1700000142 - 42 = 1700000100 (14:35:00)

# Window identifier (for all requests in 14:35:00-14:35:59)
window_id = window_start  # 1700000100

# Key for this window
key = f"ratelimit:user123:premium:{window_id}"
# → "ratelimit:user123:premium:1700000100"
```

**Why Not Use Timestamp Directly?**
```python
# BAD: Full timestamp (changes every second)
key = f"ratelimit:user123:{current_time}"
# → "ratelimit:user123:1700000142"
# → "ratelimit:user123:1700000143" ← Different key!

# GOOD: Window start (same for entire window)
key = f"ratelimit:user123:{window_start}"
# → "ratelimit:user123:1700000100" (all requests in 14:35:xx)
```

### Token Bucket: Persistent Keys

**Pattern**: `{prefix}:{identifier}:{tenant}:bucket`

**Example**: `ratelimit:user123:premium:bucket`

**Why Static?**
- Bucket persists across time
- Continuously refills regardless of requests
- No window boundaries

**Storage** (Redis Hash):
```
KEY: ratelimit:user123:premium:bucket
HASH FIELDS:
  tokens: 85000         (85 tokens remaining)
  last_refill: 1700000142  (Unix timestamp of last refill)
```

**Why Hash Instead of String?**
```python
# String approach (BAD - needs 2 keys)
tokens_key = "ratelimit:user123:bucket:tokens"
time_key = "ratelimit:user123:bucket:time"

# Hash approach (GOOD - 1 key, 2 fields)
key = "ratelimit:user123:bucket"
HMSET key tokens 85000 last_refill 1700000142
```

**Benefits of Hash**:
- Atomic update of both fields (HMSET)
- Single TTL for both fields
- More memory-efficient

### Sliding Window: Dual Time-Based Keys

**Pattern**: `{prefix}:{identifier}:{tenant}:sliding:{window_timestamp}`

**Example**:
```
Current window:  ratelimit:user123:premium:sliding:1700000100
Previous window: ratelimit:user123:premium:sliding:1700000040
```

**Why Two Keys?**
- Needs current window count
- Needs previous window count
- Weighted average: `(previous × weight) + current`

**Key Calculation**:
```python
current_time = 1700000130  # 14:35:30
window_seconds = 60

# Current window (14:35:00 - 14:35:59)
window_start = current_time - (current_time % window_seconds)
# 1700000130 - 30 = 1700000100

current_key = f"ratelimit:user123:premium:sliding:{window_start}"
# → "ratelimit:user123:premium:sliding:1700000100"

# Previous window (14:34:00 - 14:34:59)
previous_window_start = window_start - window_seconds
# 1700000100 - 60 = 1700000040

previous_key = f"ratelimit:user123:premium:sliding:{previous_window_start}"
# → "ratelimit:user123:premium:sliding:1700000040"
```

**Why Not Store in Single Key?**
```lua
-- BAD: Trying to store in one key
SET key "current:40,previous:80"  -- String parsing nightmare

-- GOOD: Separate keys
GET current_key   → 40
GET previous_key  → 80
-- Clean, simple, atomic
```

### Key Hashing for Length Limits

**Problem**: Redis keys should be under 512 bytes for performance.

**Long Key Example**:
```python
identifier = "user_with_very_long_email_address@subdomain.company.example.com"
tenant = "premium_enterprise_yearly_subscription"
# Key could be > 200 characters!
```

**Solution**: Hash long keys.

```python
def hash_key(key: str, max_length: int = 200) -> str:
    """Hash long keys to stay under max_length."""
    if len(key) <= max_length:
        return key

    # Keep prefix for debugging
    prefix = key[:max_length // 2]

    # Hash the rest
    suffix_hash = hashlib.sha256(key.encode()).hexdigest()[:16]

    return f"{prefix}:{suffix_hash}"
```

**Example**:
```python
long_key = "ratelimit:user_with_very_long_email@subdomain.company.example.com:premium_enterprise:1700000100"
# Length: 103 characters

hashed_key = hash_key(long_key, max_length=200)
# → "ratelimit:user_with_very_long_email@subdomain.company.example.com:premium_enterprise:1700000100"
# Still fits, no hashing needed

very_long_key = "ratelimit:" + ("x" * 300) + ":premium:1700000100"
# Length: 328 characters

hashed_key = hash_key(very_long_key, max_length=200)
# → "ratelimit:xxxxx...xxxxx:a1b2c3d4e5f6g7h8"
# Length: 100 characters (prefix + hash)
```

---

## Backend Design

### Architecture: Single Backend, Multiple Algorithms

```python
RedisBackend
├── connect()                    # Connection management
├── close()
├── check_fixed_window()         # Algorithm-specific methods
├── check_token_bucket()
├── check_sliding_window()
├── reset()                      # Utility methods
├── get_usage()
└── health_check()
```

**Design Principle**: **Backend is algorithm-agnostic**. It provides primitive operations, and algorithms use them.

### Connection Management

**Decision**: Use connection pooling with lazy connection.

```python
class RedisBackend:
    def __init__(self, config: RateLimitConfig):
        self.config = config
        self._redis: Optional[redis.Redis] = None  # Not connected yet!

    async def connect(self):
        """Lazy connection - only connect when needed."""
        if self._redis is not None:
            return  # Already connected

        self._redis = await redis.from_url(
            self.config.redis_url,
            encoding="utf-8",
            decode_responses=False,  # We handle encoding
        )

        # Load Lua scripts into Redis
        await self._load_scripts()
```

**Why Lazy Connection?**
1. **Faster Initialization**: Creating `RateLimiter()` doesn't block
2. **Test Friendly**: Can create limiter without Redis running
3. **Resource Efficient**: Only connect if actually used

**Connection Pooling**:
```python
# redis-py automatically uses connection pooling
redis.from_url(url)  # Creates ConnectionPool internally
```

**Benefits**:
- Reuses connections across requests
- Handles connection failures gracefully
- Automatic reconnection

### Script Loading Strategy

**Decision**: Load all scripts on connect, cache SHAs.

```python
async def _load_scripts(self):
    """Load all Lua scripts and cache their SHAs."""
    self._scripts = {
        "fixed_window": self._load_script_file("fixed_window.lua"),
        "token_bucket": self._load_script_file("token_bucket.lua"),
        "sliding_window": self._load_script_file("sliding_window.lua"),
    }

    self._script_shas = {}
    for name, script in self._scripts.items():
        sha = await self._redis.script_load(script)
        self._script_shas[name] = sha
        logger.info(f"Loaded {name} script: {sha}")
```

**Why Load on Connect?**
- Scripts available for all future requests (fast path)
- Fail fast if scripts have syntax errors
- Centralized error handling

**Fallback Mechanism**:
```python
async def _execute_fixed_window_script(self, ...):
    try:
        # Try cached version (EVALSHA)
        return await self._redis.evalsha(
            self._script_shas["fixed_window"], ...
        )
    except redis.exceptions.NoScriptError:
        # Script evicted from Redis, reload
        logger.warning("Script evicted, reloading")
        sha = await self._redis.script_load(self._scripts["fixed_window"])
        self._script_shas["fixed_window"] = sha
        return await self._redis.evalsha(sha, ...)
```

**When Scripts Get Evicted**:
- Redis SCRIPT FLUSH (manual or during maintenance)
- Redis restart
- Memory pressure (rare, scripts are small)

### Error Handling

**Decision**: Fail fast with descriptive exceptions.

```python
class BackendError(Exception):
    """Base exception for backend errors."""
    pass

class ConnectionError(BackendError):
    """Failed to connect to Redis."""
    pass

class ScriptError(BackendError):
    """Lua script execution failed."""
    pass
```

**Implementation**:
```python
async def check_fixed_window(self, key, max_requests, window_seconds, cost):
    try:
        result = await self._execute_fixed_window_script(...)
        return self._parse_result(result)
    except redis.exceptions.ConnectionError as e:
        raise ConnectionError(f"Redis connection failed: {e}") from e
    except redis.exceptions.ResponseError as e:
        raise ScriptError(f"Script execution failed: {e}") from e
    except Exception as e:
        raise BackendError(f"Unexpected error: {e}") from e
```

**Why Custom Exceptions?**
- Clearer error messages for users
- Allows caller to handle specific errors
- Hides Redis implementation details

---

## Multi-Tenant Architecture

### Decision: Support Multi-Tenancy with Tenant Types

**Use Case**: Different users have different rate limits.

**Example**:
```
Free users:    100 requests/hour
Premium users: 1000 requests/hour
Enterprise:    10000 requests/hour
```

### Implementation: Tenant Type in Key

```python
def generate_key(
    prefix: str,
    identifier: str,
    tenant_type: str,
    time_window: str
) -> str:
    """
    Generate rate limit key with tenant isolation.

    Examples:
        >>> generate_key("ratelimit", "user123", "free", "1700000100")
        'ratelimit:user123:free:1700000100'

        >>> generate_key("ratelimit", "user123", "premium", "1700000100")
        'ratelimit:user123:premium:1700000100'
    """
    safe_id = identifier.replace(":", "_").replace(" ", "_")
    safe_tenant = tenant_type.replace(":", "_").replace(" ", "_")
    full_key = f"{prefix}:{safe_id}:{safe_tenant}:{time_window}"
    return hash_key(full_key, max_length=200)
```

**Key Isolation**:
```
User 123 (Free):     ratelimit:user123:free:1700000100
User 123 (Premium):  ratelimit:user123:premium:1700000100
                                         ^^^^^^^ Different key!
```

**Usage**:
```python
# Free user
await limiter.check(
    key="user123",
    rate="100/hour",
    tenant_type="free"
)

# Premium user (upgraded)
await limiter.check(
    key="user123",
    rate="1000/hour",
    tenant_type="premium"
)
```

### Tenant Management Patterns

**Pattern 1: Request-Based Tenant Detection**
```python
@app.get("/api/data")
@limiter.limit(
    "100/hour",
    tenant_type=lambda req: req.user.subscription_tier
)
async def get_data(request: Request):
    return {"data": "..."}
```

**Pattern 2: Middleware-Based Tenant Injection**
```python
class TenantMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        user = await get_current_user(request)
        request.state.tenant_type = user.subscription_tier
        return await call_next(request)

@app.get("/api/data")
@limiter.limit(
    "100/hour",
    tenant_type=lambda req: req.state.tenant_type
)
async def get_data(request: Request):
    return {"data": "..."}
```

**Pattern 3: Manual Tenant Specification**
```python
user = await get_current_user(request)
tenant_type = user.subscription_tier

await limiter.check(
    key=f"user:{user.id}",
    rate=get_rate_for_tier(tenant_type),
    tenant_type=tenant_type
)
```

---

## Async Python Design

### Decision: Async-First API

**Question**: Why not sync API with optional async?

**Answer**: Modern Python is async-first, and rate limiting is I/O-bound.

### Benefits of Async

**Performance**: Handle thousands of concurrent requests.

```python
# Sync version (BAD for web apps)
def check(self, key, rate):
    result = redis.get(key)  # Blocks entire thread!
    # Other requests wait...

# Async version (GOOD)
async def check(self, key, rate):
    result = await redis.get(key)  # Only blocks this coroutine!
    # Other requests proceed concurrently
```

**Scalability**:
```
Sync (Threading):
  1000 concurrent requests = 1000 threads = ~1 GB memory

Async (Coroutines):
  1000 concurrent requests = 1000 coroutines = ~10 MB memory
```

### Async Context Manager

```python
class RateLimiter:
    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

# Usage:
async with RateLimiter() as limiter:
    await limiter.check("user:123", "100/minute")
# Automatically closes connection
```

**Benefits**:
- Guarantees connection cleanup
- Exception-safe
- Pythonic

### Connection Lifecycle

**Manual Management**:
```python
limiter = RateLimiter()
await limiter.connect()
try:
    await limiter.check("user:123", "100/minute")
finally:
    await limiter.close()
```

**Context Manager** (Recommended):
```python
async with RateLimiter() as limiter:
    await limiter.check("user:123", "100/minute")
```

**Global Instance** (FastAPI):
```python
limiter = RateLimiter()

@app.on_event("startup")
async def startup():
    await limiter.connect()

@app.on_event("shutdown")
async def shutdown():
    await limiter.close()

@app.get("/api/data")
async def get_data():
    await limiter.check("user:123", "100/minute")
```

---

## Error Handling Philosophy

### Decision: Explicit, Descriptive Errors

**Principle**: **Fail fast and loud**. Never silently ignore errors.

### Exception Hierarchy

```python
RateLimitError (Base)
├── RateLimitExceeded      # Expected (client exceeded limit)
├── RateLimitConfigError   # Configuration issue
└── BackendError           # Redis/backend issue
    ├── ConnectionError
    ├── ScriptError
    └── TimeoutError
```

### RateLimitExceeded (Expected Error)

**When**: Client exceeds their rate limit.

**Response**: Return 429 with Retry-After header.

```python
try:
    await limiter.check("user:123", "100/minute")
except RateLimitExceeded as e:
    # Expected error - client hit limit
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "retry_after": e.retry_after,
            "limit": e.limit,
        },
        headers={"Retry-After": str(e.retry_after)}
    )
```

### RateLimitConfigError (Developer Error)

**When**: Invalid configuration (wrong rate format, unknown algorithm).

**Response**: Fail fast during development, not in production.

```python
try:
    await limiter.check("user:123", rate="invalid_rate")
except RateLimitConfigError as e:
    # Developer error - fix the code!
    logger.error(f"Configuration error: {e}")
    # In development: Re-raise to catch bugs
    # In production: Return 500 (should never happen)
    raise
```

### BackendError (Infrastructure Issue)

**When**: Redis connection failure, timeout, etc.

**Response**: Depends on requirements.

**Option 1: Fail Closed (Secure)**
```python
try:
    await limiter.check("user:123", "100/minute")
except BackendError as e:
    # Redis is down - deny all requests
    logger.error(f"Backend error: {e}")
    return JSONResponse(
        status_code=503,
        content={"error": "Service temporarily unavailable"}
    )
```

**Option 2: Fail Open (Available)**
```python
try:
    await limiter.check("user:123", "100/minute")
except BackendError as e:
    # Redis is down - allow requests (risk of abuse)
    logger.error(f"Backend error, allowing request: {e}")
    # Continue processing
```

**Recommendation**: Fail closed for security, fail open for availability.

---

## Testing Strategy

### Test Pyramid

```
     /\
    /  \  E2E Tests (5%)
   /    \
  /------\ Integration Tests (25%)
 /        \
/----------\ Unit Tests (70%)
```

### Unit Tests: Algorithm Logic

**Test**: Algorithm implementations without Redis.

```python
def test_sliding_window_weight_calculation():
    """Test weighted average formula."""
    current_count = 40
    previous_count = 80
    window_seconds = 60
    elapsed_seconds = 30

    weighted = calculate_sliding_window_count(
        current_count, previous_count, window_seconds, elapsed_seconds
    )

    # Weight: 1 - (30/60) = 0.5
    # Weighted: 40 + (80 * 0.5) = 80
    assert weighted == 80.0
```

### Integration Tests: Redis Operations

**Test**: Full rate limiting flow with real Redis.

```python
@pytest.mark.asyncio
async def test_rate_limit_enforcement():
    """Test that rate limiting actually works."""
    limiter = RateLimiter(redis_url="redis://localhost:6379/15")
    await limiter.connect()

    # Make 100 requests (all should pass)
    for i in range(100):
        await limiter.check("test:user", "100/minute")

    # 101st request should fail
    with pytest.raises(RateLimitExceeded):
        await limiter.check("test:user", "100/minute")

    await limiter.close()
```

### Concurrency Tests: Race Conditions

**Test**: Multiple concurrent requests don't exceed limit.

```python
@pytest.mark.asyncio
async def test_concurrent_requests():
    """Test thread-safety with concurrent requests."""
    limiter = RateLimiter()
    await limiter.connect()

    async def make_request():
        try:
            await limiter.check("test:concurrent", "100/minute")
            return 1
        except RateLimitExceeded:
            return 0

    # 200 concurrent requests
    tasks = [make_request() for _ in range(200)]
    results = await asyncio.gather(*tasks)

    # Exactly 100 should succeed
    assert sum(results) == 100

    await limiter.close()
```

---

## Metrics & Observability

### Decision: Optional Prometheus Metrics

**Why Optional?**
- Not all users need metrics
- Avoids required dependency
- Graceful degradation

### Implementation

```python
try:
    from prometheus_client import Counter, Histogram, Gauge
    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

class RateLimitMetrics:
    def __init__(self, enabled: bool = True):
        if not enabled or not PROMETHEUS_AVAILABLE:
            self._enabled = False
            return

        self._enabled = True
        self.checks_total = Counter(...)
        self.checks_duration = Histogram(...)
        # ... more metrics
```

### Metrics Collected

1. **checks_total**: Total rate limit checks (by algorithm, tenant, result)
2. **checks_duration_seconds**: Latency distribution
3. **limit_exceeded_total**: How many times limits were hit
4. **algorithm_usage**: Which algorithms are used most
5. **backend_errors_total**: Redis errors
6. **active_keys**: Number of active rate limit keys

### Usage

```python
limiter = RateLimiter(enable_metrics=True)

# Metrics automatically collected
await limiter.check("user:123", "100/minute")

# Expose metrics endpoint
from prometheus_client import generate_latest

@app.get("/metrics")
async def metrics():
    return Response(
        generate_latest(),
        media_type="text/plain"
    )
```

---

## Performance Optimizations

### 1. Script Caching (EVALSHA)

**Impact**: ~40% faster than EVAL.

**Implementation**: Covered in [Why Lua Scripts?](#why-lua-scripts)

### 2. Connection Pooling

**Impact**: Reuse connections, avoid handshake overhead.

**Implementation**: Built into redis-py.

### 3. Pipeline Optimization (Future Work)

**Opportunity**: Batch multiple checks.

```python
# Current: 3 round trips
await limiter.check("user1", "100/min")  # Round trip 1
await limiter.check("user2", "100/min")  # Round trip 2
await limiter.check("user3", "100/min")  # Round trip 3

# Future: 1 round trip with pipeline
async with limiter.pipeline() as pipe:
    pipe.check("user1", "100/min")
    pipe.check("user2", "100/min")
    pipe.check("user3", "100/min")
    results = await pipe.execute()
```

### 4. Key Hashing

**Impact**: Faster Redis operations with shorter keys.

**Implementation**: Covered in [Key Naming Architecture](#key-naming-architecture).

---

## Security Considerations

### 1. Prevent Key Injection

**Vulnerability**: User-controlled identifiers could include colons.

```python
# Malicious input
user_id = "user:123:admin:999999999"

# Without sanitization
key = f"ratelimit:{user_id}:free:1700000100"
# → "ratelimit:user:123:admin:999999999:free:1700000100"
# Could collide with other keys!
```

**Mitigation**:
```python
def generate_key(...):
    safe_id = identifier.replace(":", "_").replace(" ", "_")
    safe_tenant = tenant_type.replace(":", "_").replace(" ", "_")
    # Now safe from injection
```

### 2. TTL Enforcement

**Risk**: Keys without TTL leak memory.

**Mitigation**: Always set TTL, with fallback.

```lua
if current == cost then
    redis.call('EXPIRE', key, window_seconds)
end

-- Safety check
local ttl = redis.call('TTL', key)
if ttl <= 0 then
    redis.call('EXPIRE', key, window_seconds)
end
```

### 3. Integer Overflow Protection

**Risk**: Large costs could overflow.

**Mitigation**: Validate inputs.

```python
if cost < 0 or cost > 1000000:
    raise RateLimitConfigError(f"Invalid cost: {cost}")
```

---

## Design Trade-offs

### Trade-off 1: Accuracy vs Performance

**Choice**: Offer multiple algorithms.

- Fixed Window: Fast, less accurate
- Token Bucket: Balanced
- Sliding Window: Accurate, slower

**Rationale**: Let users choose based on needs.

### Trade-off 2: Fail Open vs Fail Closed

**Choice**: Let users decide via exception handling.

**Rationale**: Different apps have different priorities (security vs availability).

### Trade-off 3: Redis vs Other Backends

**Choice**: Redis only (for now).

**Rationale**: 99% of use cases, better to do one thing well.

**Future**: Could add PostgreSQL backend for persistence.

---

## Summary

FastLimit's architecture prioritizes:

1. **Correctness**: Atomic operations, no race conditions
2. **Performance**: Lua scripts, connection pooling, caching
3. **Flexibility**: Multiple algorithms, multi-tenancy, cost-based limiting
4. **Reliability**: Graceful degradation, comprehensive error handling
5. **Simplicity**: Clean API, async-first, minimal dependencies

Every design decision balances these priorities for production-ready rate limiting.
