-- Token Bucket Rate Limiting Script
-- Implements token bucket algorithm with atomic operations
--
-- KEYS[1] = rate limit key (e.g., "ratelimit:tenant123:premium:bucket")
-- ARGV[1] = max_tokens (bucket capacity, e.g., 100000 for 100 tokens with 1000x multiplier)
-- ARGV[2] = refill_rate (tokens per second, e.g., 1667 for ~1.67/sec with 1000x multiplier)
-- ARGV[3] = current_timestamp (seconds since epoch)
-- ARGV[4] = cost (tokens to consume, e.g., 1000 for cost=1 with 1000x multiplier)
--
-- Returns: {allowed (1 or 0), remaining, retry_after_ms}
--
-- Token Bucket Algorithm:
-- - Tokens are continuously added at refill_rate
-- - Bucket has maximum capacity of max_tokens
-- - Each request consumes 'cost' tokens
-- - If not enough tokens, request is denied
-- - Provides smoother rate limiting than fixed window

local key = KEYS[1]
local max_tokens = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])
local current_time = tonumber(ARGV[3])
local cost = tonumber(ARGV[4]) or 1000  -- Default to 1000 (cost=1) if not provided

-- Get current bucket state
-- Redis HGETALL returns: {field1, value1, field2, value2, ...}
local bucket = redis.call('HMGET', key, 'tokens', 'last_refill')
local current_tokens = tonumber(bucket[1]) or max_tokens  -- Start with full bucket
local last_refill = tonumber(bucket[2]) or current_time

-- Calculate time elapsed since last refill
local time_elapsed = math.max(0, current_time - last_refill)

-- Calculate tokens to add based on elapsed time
-- tokens_to_add = refill_rate * time_elapsed
local tokens_to_add = refill_rate * time_elapsed

-- Add tokens to bucket, but don't exceed max capacity
local new_tokens = math.min(max_tokens, current_tokens + tokens_to_add)

-- Determine if request is allowed
local allowed = 0
local remaining = 0
local retry_after_ms = 0

if new_tokens >= cost then
    -- Request is allowed - consume tokens
    allowed = 1
    new_tokens = new_tokens - cost
    remaining = new_tokens

    -- Update bucket state
    redis.call('HMSET', key, 'tokens', new_tokens, 'last_refill', current_time)

    -- Set expiry to prevent memory leaks
    -- Expire after bucket would be completely refilled (from empty)
    local ttl = math.ceil(max_tokens / refill_rate) + 60  -- Add 60s buffer
    redis.call('EXPIRE', key, ttl)
else
    -- Request is denied - not enough tokens
    allowed = 0
    remaining = 0

    -- Calculate how long until enough tokens are available
    local tokens_needed = cost - new_tokens
    local time_needed = math.ceil(tokens_needed / refill_rate)
    retry_after_ms = time_needed * 1000

    -- Update bucket state with refilled tokens (even though request denied)
    -- This ensures the next request has accurate token count
    redis.call('HMSET', key, 'tokens', new_tokens, 'last_refill', current_time)

    -- Set expiry
    local ttl = math.ceil(max_tokens / refill_rate) + 60
    redis.call('EXPIRE', key, ttl)
end

-- Return results
-- allowed: 1 if request should proceed, 0 if rate limited
-- remaining: number of tokens remaining in bucket (with multiplier)
-- retry_after_ms: milliseconds until enough tokens available (0 if allowed)
return {allowed, remaining, retry_after_ms}
