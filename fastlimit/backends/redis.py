"""
Redis backend implementation for rate limiting.
"""

import redis.asyncio as redis
from pathlib import Path
from typing import Optional, NamedTuple, Dict, Any
import logging
import time

from ..models import RateLimitConfig
from ..exceptions import BackendError

logger = logging.getLogger(__name__)


class RateLimitResult(NamedTuple):
    """Result of a rate limit check."""

    allowed: bool  # Whether the request is allowed
    remaining: int  # Number of requests remaining (with multiplier)
    retry_after: int  # Milliseconds until rate limit resets


class RedisBackend:
    """
    Redis backend with Lua script support for atomic rate limiting.

    This backend uses Lua scripts to ensure atomic operations,
    preventing race conditions in distributed environments.
    """

    def __init__(self, config: RateLimitConfig):
        """
        Initialize Redis backend.

        Args:
            config: Rate limiter configuration
        """
        self.config = config
        self._redis: Optional[redis.Redis] = None
        self._scripts: Dict[str, str] = {}
        self._script_shas: Dict[str, str] = {}
        self._connected = False
        self._load_scripts()

    def _load_scripts(self) -> None:
        """Load Lua scripts from files or use inline defaults."""
        script_dir = Path(__file__).parent.parent / "scripts"

        # Load fixed window script
        fixed_window_path = script_dir / "fixed_window.lua"
        if fixed_window_path.exists():
            with open(fixed_window_path, "r") as f:
                self._scripts["fixed_window"] = f.read()
        else:
            # Fallback inline script if file doesn't exist
            self._scripts["fixed_window"] = """
-- Fixed Window Rate Limiting Script (Inline Fallback)
local key = KEYS[1]
local max_requests = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])

local current = redis.call('INCR', key)

if current == 1 then
    redis.call('EXPIRE', key, window_seconds)
end

local ttl = redis.call('TTL', key)
if ttl < 0 then
    ttl = window_seconds
end

local allowed = 0
local remaining = 0

if current <= max_requests then
    allowed = 1
    remaining = max_requests - current
else
    remaining = 0
end

return {allowed, remaining, ttl * 1000}
"""

        # Load token bucket script
        token_bucket_path = script_dir / "token_bucket.lua"
        if token_bucket_path.exists():
            with open(token_bucket_path, "r") as f:
                self._scripts["token_bucket"] = f.read()
        else:
            # No fallback for token bucket - requires file
            logger.warning("token_bucket.lua not found, token bucket algorithm disabled")

        # Load sliding window script
        sliding_window_path = script_dir / "sliding_window.lua"
        if sliding_window_path.exists():
            with open(sliding_window_path, "r") as f:
                self._scripts["sliding_window"] = f.read()
        else:
            # No fallback for sliding window - requires file
            logger.warning("sliding_window.lua not found, sliding window algorithm disabled")

        logger.debug(f"Loaded {len(self._scripts)} Lua scripts")

    async def connect(self) -> None:
        """
        Initialize Redis connection and load scripts.

        Raises:
            BackendError: If connection fails
        """
        if self._connected:
            logger.debug("Already connected to Redis")
            return

        try:
            # Create Redis connection with connection pooling
            self._redis = redis.from_url(
                self.config.redis_url,
                encoding="utf-8",
                decode_responses=False,  # We handle decoding ourselves for better control
                socket_connect_timeout=self.config.connection_timeout,
                socket_timeout=self.config.socket_timeout,
                max_connections=self.config.max_connections,
            )

            # Test connection
            await self._redis.ping()

            # Load scripts into Redis for better performance
            await self._register_scripts()

            self._connected = True
            logger.info(f"Connected to Redis at {self.config.redis_url}")

        except redis.ConnectionError as e:
            raise BackendError(f"Failed to connect to Redis: {e}")
        except Exception as e:
            raise BackendError(f"Unexpected error during Redis connection: {e}")

    async def _register_scripts(self) -> None:
        """Register Lua scripts with Redis for optimal performance."""
        if not self._redis:
            return

        for name, script in self._scripts.items():
            try:
                # Register script and store SHA for EVALSHA calls
                sha = await self._redis.script_load(script)
                self._script_shas[name] = sha
                logger.debug(f"Registered script '{name}' with SHA: {sha}")
            except Exception as e:
                logger.warning(f"Failed to register script '{name}': {e}")
                # Script will be executed with EVAL instead of EVALSHA

    async def close(self) -> None:
        """Close Redis connection gracefully."""
        if self._redis and self._connected:
            await self._redis.close()
            self._connected = False
            logger.info("Closed Redis connection")

    async def check_fixed_window(
        self, key: str, max_requests: int, window_seconds: int, cost: int = 1000
    ) -> RateLimitResult:
        """
        Check rate limit using fixed window algorithm.

        This method executes the fixed window Lua script atomically,
        ensuring thread-safe rate limiting even in distributed systems.

        Args:
            key: Rate limit key (should be pre-formatted)
            max_requests: Maximum requests allowed (with 1000x multiplier)
            window_seconds: Size of the time window in seconds
            cost: Cost of this request (with 1000x multiplier, default 1000 = cost of 1)

        Returns:
            RateLimitResult with allowed status and metadata

        Raises:
            BackendError: If Redis operation fails
        """
        if not self._redis or not self._connected:
            raise BackendError("Redis not connected. Call connect() first.")

        try:
            # Try EVALSHA first for better performance
            if "fixed_window" in self._script_shas:
                try:
                    result = await self._redis.evalsha(
                        self._script_shas["fixed_window"],
                        1,  # number of keys
                        key.encode(),  # KEYS[1]
                        str(max_requests).encode(),  # ARGV[1]
                        str(window_seconds).encode(),  # ARGV[2]
                        str(int(time.time())).encode(),  # ARGV[3] - timestamp
                        str(cost).encode(),  # ARGV[4] - cost
                    )
                except redis.NoScriptError:
                    # Script not in cache, fall back to EVAL
                    logger.debug("Script not in cache, using EVAL")
                    result = await self._execute_script("fixed_window", key, max_requests, window_seconds, cost)
            else:
                # No SHA available, use EVAL
                result = await self._execute_script("fixed_window", key, max_requests, window_seconds, cost)

            # Parse result
            if not isinstance(result, list) or len(result) != 3:
                raise BackendError(f"Invalid script result: {result}")

            allowed = bool(int(result[0]))
            remaining = int(result[1])
            retry_after_ms = int(result[2])

            return RateLimitResult(
                allowed=allowed,
                remaining=remaining,
                retry_after=retry_after_ms,
            )

        except redis.RedisError as e:
            logger.error(f"Redis error during rate limit check: {e}")
            raise BackendError(f"Rate limit check failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error during rate limit check: {e}")
            raise BackendError(f"Unexpected error: {e}")

    async def _execute_script(
        self, script_name: str, key: str, max_requests: int, window_seconds: int, cost: int = 1000
    ) -> Any:
        """Execute Lua script with EVAL."""
        if not self._redis:
            raise BackendError("Redis not connected")

        script = self._scripts.get(script_name)
        if not script:
            raise BackendError(f"Script '{script_name}' not found")

        return await self._redis.eval(
            script,
            1,  # number of keys
            key.encode(),  # KEYS[1]
            str(max_requests).encode(),  # ARGV[1]
            str(window_seconds).encode(),  # ARGV[2]
            str(int(time.time())).encode(),  # ARGV[3]
            str(cost).encode(),  # ARGV[4]
        )

    async def check_token_bucket(
        self, key: str, max_tokens: int, refill_rate: float, current_time: int, cost: int = 1000
    ) -> RateLimitResult:
        """
        Check rate limit using token bucket algorithm.

        This method executes the token bucket Lua script atomically,
        ensuring thread-safe rate limiting with smooth token refills.

        Args:
            key: Rate limit key (should be pre-formatted)
            max_tokens: Maximum bucket capacity (with 1000x multiplier)
            refill_rate: Tokens added per second (with 1000x multiplier)
            current_time: Current Unix timestamp in seconds
            cost: Tokens to consume (with 1000x multiplier, default 1000)

        Returns:
            RateLimitResult with allowed status and metadata

        Raises:
            BackendError: If Redis operation fails
        """
        if not self._redis or not self._connected:
            raise BackendError("Redis not connected. Call connect() first.")

        try:
            # Try EVALSHA first for better performance
            if "token_bucket" in self._script_shas:
                try:
                    result = await self._redis.evalsha(
                        self._script_shas["token_bucket"],
                        1,  # number of keys
                        key.encode(),  # KEYS[1]
                        str(int(max_tokens)).encode(),  # ARGV[1]
                        str(int(refill_rate)).encode(),  # ARGV[2] - convert to int
                        str(current_time).encode(),  # ARGV[3]
                        str(cost).encode(),  # ARGV[4]
                    )
                except redis.NoScriptError:
                    # Script not in cache, fall back to EVAL
                    logger.debug("Script not in cache, using EVAL")
                    result = await self._execute_token_bucket_script(
                        key, max_tokens, refill_rate, current_time, cost
                    )
            else:
                # No SHA available, use EVAL
                result = await self._execute_token_bucket_script(
                    key, max_tokens, refill_rate, current_time, cost
                )

            # Parse result
            if not isinstance(result, list) or len(result) != 3:
                raise BackendError(f"Invalid script result: {result}")

            allowed = bool(int(result[0]))
            remaining = int(result[1])
            retry_after_ms = int(result[2])

            return RateLimitResult(
                allowed=allowed,
                remaining=remaining,
                retry_after=retry_after_ms,
            )

        except redis.RedisError as e:
            logger.error(f"Redis error during token bucket check: {e}")
            raise BackendError(f"Token bucket check failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error during token bucket check: {e}")
            raise BackendError(f"Unexpected error: {e}")

    async def _execute_token_bucket_script(
        self, key: str, max_tokens: int, refill_rate: float, current_time: int, cost: int = 1000
    ) -> Any:
        """Execute token bucket Lua script with EVAL."""
        if not self._redis:
            raise BackendError("Redis not connected")

        script = self._scripts.get("token_bucket")
        if not script:
            raise BackendError("Token bucket script not loaded")

        return await self._redis.eval(
            script,
            1,  # number of keys
            key.encode(),  # KEYS[1]
            str(int(max_tokens)).encode(),  # ARGV[1]
            str(int(refill_rate)).encode(),  # ARGV[2]
            str(current_time).encode(),  # ARGV[3]
            str(cost).encode(),  # ARGV[4]
        )

    async def get_token_bucket_usage(self, key: str) -> Dict[str, Any]:
        """
        Get current token bucket usage statistics.

        Args:
            key: Rate limit key to check

        Returns:
            Dictionary with tokens and last_refill timestamp

        Raises:
            BackendError: If Redis operation fails
        """
        if not self._redis or not self._connected:
            raise BackendError("Redis not connected")

        try:
            # Use HMGET to get bucket state
            result = await self._redis.hmget(key, "tokens", "last_refill")

            tokens = int(result[0]) if result[0] else 0
            last_refill = int(result[1]) if result[1] else 0

            return {
                "tokens": tokens,
                "last_refill": last_refill,
            }
        except redis.RedisError as e:
            logger.error(f"Failed to get token bucket usage for key {key}: {e}")
            raise BackendError(f"Failed to get usage statistics: {e}")

    async def check_sliding_window(
        self,
        current_key: str,
        previous_key: str,
        max_requests: int,
        window_seconds: int,
        current_time: int,
        cost: int = 1000,
    ) -> RateLimitResult:
        """
        Check rate limit using sliding window algorithm.

        This method executes the sliding window Lua script atomically,
        combining current and previous windows with weighted average.

        Args:
            current_key: Redis key for current window
            previous_key: Redis key for previous window
            max_requests: Maximum requests allowed (with 1000x multiplier)
            window_seconds: Size of the time window in seconds
            current_time: Current Unix timestamp in seconds
            cost: Tokens to consume (with 1000x multiplier, default 1000)

        Returns:
            RateLimitResult with allowed status and metadata

        Raises:
            BackendError: If Redis operation fails
        """
        if not self._redis or not self._connected:
            raise BackendError("Redis not connected. Call connect() first.")

        try:
            # Try EVALSHA first for better performance
            if "sliding_window" in self._script_shas:
                try:
                    result = await self._redis.evalsha(
                        self._script_shas["sliding_window"],
                        2,  # number of keys (current + previous)
                        current_key.encode(),  # KEYS[1]
                        previous_key.encode(),  # KEYS[2]
                        str(int(max_requests)).encode(),  # ARGV[1]
                        str(window_seconds).encode(),  # ARGV[2]
                        str(current_time).encode(),  # ARGV[3]
                        str(cost).encode(),  # ARGV[4]
                    )
                except redis.NoScriptError:
                    # Script not in cache, fall back to EVAL
                    logger.debug("Script not in cache, using EVAL")
                    result = await self._execute_sliding_window_script(
                        current_key, previous_key, max_requests, window_seconds, current_time, cost
                    )
            else:
                # No SHA available, use EVAL
                result = await self._execute_sliding_window_script(
                    current_key, previous_key, max_requests, window_seconds, current_time, cost
                )

            # Parse result
            if not isinstance(result, list) or len(result) != 3:
                raise BackendError(f"Invalid script result: {result}")

            allowed = bool(int(result[0]))
            remaining = int(result[1])
            retry_after_ms = int(result[2])

            return RateLimitResult(
                allowed=allowed,
                remaining=remaining,
                retry_after=retry_after_ms,
            )

        except redis.RedisError as e:
            logger.error(f"Redis error during sliding window check: {e}")
            raise BackendError(f"Sliding window check failed: {e}")
        except Exception as e:
            logger.error(f"Unexpected error during sliding window check: {e}")
            raise BackendError(f"Unexpected error: {e}")

    async def _execute_sliding_window_script(
        self,
        current_key: str,
        previous_key: str,
        max_requests: int,
        window_seconds: int,
        current_time: int,
        cost: int = 1000,
    ) -> Any:
        """Execute sliding window Lua script with EVAL."""
        if not self._redis:
            raise BackendError("Redis not connected")

        script = self._scripts.get("sliding_window")
        if not script:
            raise BackendError("Sliding window script not loaded")

        return await self._redis.eval(
            script,
            2,  # number of keys
            current_key.encode(),  # KEYS[1]
            previous_key.encode(),  # KEYS[2]
            str(int(max_requests)).encode(),  # ARGV[1]
            str(window_seconds).encode(),  # ARGV[2]
            str(current_time).encode(),  # ARGV[3]
            str(cost).encode(),  # ARGV[4]
        )

    async def reset(self, key: str) -> bool:
        """
        Reset rate limit for a specific key.

        Args:
            key: Rate limit key to reset

        Returns:
            True if key was deleted, False if key didn't exist

        Raises:
            BackendError: If Redis operation fails
        """
        if not self._redis or not self._connected:
            raise BackendError("Redis not connected")

        try:
            result = await self._redis.delete(key)
            return bool(result)
        except redis.RedisError as e:
            logger.error(f"Failed to reset key {key}: {e}")
            raise BackendError(f"Failed to reset rate limit: {e}")

    async def get_usage(self, key: str) -> Dict[str, Any]:
        """
        Get current usage statistics for a key.

        Args:
            key: Rate limit key to check

        Returns:
            Dictionary with current count and TTL

        Raises:
            BackendError: If Redis operation fails
        """
        if not self._redis or not self._connected:
            raise BackendError("Redis not connected")

        try:
            # Use pipeline for atomic read
            pipe = self._redis.pipeline()
            pipe.get(key)
            pipe.ttl(key)
            result = await pipe.execute()

            current = int(result[0]) if result[0] else 0
            ttl = result[1] if result[1] > 0 else 0

            return {
                "current": current,
                "ttl": ttl,
            }
        except redis.RedisError as e:
            logger.error(f"Failed to get usage for key {key}: {e}")
            raise BackendError(f"Failed to get usage statistics: {e}")

    async def health_check(self) -> bool:
        """
        Check if Redis connection is healthy.

        Returns:
            True if healthy, False otherwise
        """
        if not self._redis or not self._connected:
            return False

        try:
            await self._redis.ping()
            return True
        except:
            return False
