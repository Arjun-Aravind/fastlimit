-- Fixed Window Rate Limiting Script
-- Implements atomic rate limiting using Redis
--
-- KEYS[1] = rate limit key (e.g., "ratelimit:tenant123:free:2024-11-01-14:35")
-- ARGV[1] = max_requests (e.g., 100000 for 100 requests with 1000x multiplier)
-- ARGV[2] = window_seconds (e.g., 60 for 1 minute window)
-- ARGV[3] = current timestamp (unused in fixed window, kept for consistency)
-- ARGV[4] = cost (e.g., 1000 for cost=1 with 1000x multiplier, default 1000)
--
-- Returns: {allowed (1 or 0), remaining, retry_after_ms}

local key = KEYS[1]
local max_requests = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local cost = tonumber(ARGV[4]) or 1000  -- Default to 1000 (cost=1) if not provided

-- Increment counter atomically by cost
local current = redis.call('INCRBY', key, cost)

-- Set TTL only on first request (when counter equals cost)
-- This ensures the window expires properly
if current == cost then
    redis.call('EXPIRE', key, window_seconds)
end

-- Get TTL for retry_after calculation
local ttl = redis.call('TTL', key)

-- Handle edge cases where key might not have TTL set properly
-- TTL < 0 means key has no expiration (shouldn't happen, but be safe)
-- TTL = 0 means key doesn't exist or just expired
if ttl <= 0 then
    ttl = window_seconds
    -- Ensure TTL is set (in case EXPIRE failed earlier)
    redis.call('EXPIRE', key, window_seconds)
end

-- Calculate if request is allowed
local allowed = 0
local remaining = 0

if current <= max_requests then
    -- Request is allowed
    allowed = 1
    remaining = max_requests - current
else
    -- Request is denied, no remaining capacity
    remaining = 0
end

-- Return results
-- allowed: 1 if request should proceed, 0 if rate limited
-- remaining: number of requests remaining in current window (with multiplier)
-- retry_after_ms: milliseconds until the current window resets
return {allowed, remaining, ttl * 1000}
