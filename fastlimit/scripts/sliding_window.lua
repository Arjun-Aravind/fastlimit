-- Sliding Window Rate Limiting Script
-- Implements sliding window algorithm with weighted previous window
--
-- KEYS[1] = current window key (e.g., "ratelimit:user123:default:2024-11-19-14:35")
-- KEYS[2] = previous window key (e.g., "ratelimit:user123:default:2024-11-19-14:34")
-- ARGV[1] = max_requests (e.g., 100000 for 100 requests with 1000x multiplier)
-- ARGV[2] = window_seconds (e.g., 60 for 1 minute window)
-- ARGV[3] = current_timestamp (seconds since epoch)
-- ARGV[4] = cost (tokens to consume, e.g., 1000 for cost=1 with 1000x multiplier)
--
-- Returns: {allowed (1 or 0), remaining, retry_after_ms}
--
-- Sliding Window Algorithm:
-- - Combines current window with weighted portion of previous window
-- - Weight = percentage of current window elapsed
-- - Example: 30 seconds into 60-second window = 50% weight from previous
-- - Most accurate, no boundary bursts, smooth rate limiting
-- - Formula: weighted_count = prev_count * (1 - weight) + current_count

local current_key = KEYS[1]
local previous_key = KEYS[2]
local max_requests = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])
local current_timestamp = tonumber(ARGV[3])
local cost = tonumber(ARGV[4]) or 1000  -- Default to 1000 (cost=1) if not provided

-- Get counts from both windows
local current_count = tonumber(redis.call('GET', current_key)) or 0
local previous_count = tonumber(redis.call('GET', previous_key)) or 0

-- Calculate position in current window (0 to 1)
-- This determines how much weight to give to previous window
local window_start = current_timestamp - (current_timestamp % window_seconds)
local elapsed_in_window = current_timestamp - window_start
local window_progress = elapsed_in_window / window_seconds

-- Calculate weighted count using sliding window formula
-- As we progress through current window, previous window has less weight
-- Example: 25% into window = 75% weight from previous, 25% from current
local previous_weight = 1 - window_progress
local weighted_count = (previous_count * previous_weight) + current_count

-- Check if request is allowed (before adding cost)
local allowed = 0
local remaining = 0
local retry_after_ms = 0

if (weighted_count + cost) <= max_requests then
    -- Request is allowed - increment current window
    allowed = 1

    -- Increment current window counter
    current_count = redis.call('INCRBY', current_key, cost)

    -- Set TTL on current window (2x window to keep previous)
    redis.call('EXPIRE', current_key, window_seconds * 2)

    -- Recalculate weighted count after increment
    weighted_count = (previous_count * previous_weight) + current_count
    remaining = math.max(0, max_requests - weighted_count)
else
    -- Request is denied
    allowed = 0
    remaining = 0

    -- Calculate time until enough capacity is available
    -- Need to wait until weighted count drops below max_requests
    local tokens_needed = (weighted_count + cost) - max_requests

    -- Estimate time needed (simplified)
    -- As window progresses, previous window weight decreases
    -- Tokens free up as previous_count weight decreases
    local time_until_previous_expires = window_seconds - elapsed_in_window
    retry_after_ms = math.ceil(time_until_previous_expires * 1000)
end

-- Return results
-- allowed: 1 if request should proceed, 0 if rate limited
-- remaining: estimated tokens remaining (with multiplier)
-- retry_after_ms: milliseconds until rate limit might allow request
return {allowed, remaining, retry_after_ms}
