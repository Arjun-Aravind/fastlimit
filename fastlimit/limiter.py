"""
Main RateLimiter class implementation.
"""

import asyncio
from typing import Optional, Callable, Any, Dict
from datetime import datetime
import logging

from .backends.redis import RedisBackend
from .models import RateLimitConfig
from .exceptions import RateLimitExceeded, RateLimitConfigError
from .utils import parse_rate, generate_key, get_time_window

logger = logging.getLogger(__name__)


class RateLimiter:
    """
    Main rate limiter class with async support.

    This class provides the primary interface for rate limiting,
    supporting both decorator-based and manual check approaches.

    Examples:
        Basic usage:
        >>> limiter = RateLimiter(redis_url="redis://localhost:6379")
        >>> await limiter.connect()
        >>> await limiter.check(key="user:123", rate="100/minute")

        With context manager:
        >>> async with RateLimiter() as limiter:
        >>>     await limiter.check(key="api:endpoint", rate="1000/hour")

        As decorator:
        >>> @limiter.limit("100/minute")
        >>> async def my_endpoint(request):
        >>>     return {"status": "ok"}
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        key_prefix: str = "ratelimit",
        default_algorithm: str = "fixed_window",
        enable_metrics: bool = False,
    ):
        """
        Initialize the rate limiter.

        Args:
            redis_url: Redis connection URL
            key_prefix: Prefix for all Redis keys
            default_algorithm: Default algorithm to use
            enable_metrics: Whether to enable metrics collection
        """
        self.config = RateLimitConfig(
            redis_url=redis_url,
            key_prefix=key_prefix,
            default_algorithm=default_algorithm,
            enable_metrics=enable_metrics,
        )
        self.backend = RedisBackend(self.config)
        self._connected = False
        self._lock = asyncio.Lock()  # For thread-safe connection

        logger.debug(f"Initialized RateLimiter with config: {self.config}")

    async def __aenter__(self):
        """Async context manager entry."""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

    async def connect(self) -> None:
        """
        Initialize Redis connection.

        This method is idempotent and thread-safe.

        Raises:
            BackendError: If connection fails
        """
        async with self._lock:
            if not self._connected:
                await self.backend.connect()
                self._connected = True
                logger.info("RateLimiter connected to Redis")

    async def close(self) -> None:
        """
        Close Redis connection gracefully.

        This method is idempotent and thread-safe.
        """
        async with self._lock:
            if self._connected:
                await self.backend.close()
                self._connected = False
                logger.info("RateLimiter disconnected from Redis")

    async def check(
        self,
        key: str,
        rate: str,
        algorithm: Optional[str] = None,
        tenant_type: Optional[str] = None,
        cost: int = 1,
    ) -> bool:
        """
        Check if a request is allowed under the rate limit.

        This is the core method for rate limiting. It checks whether
        a request identified by `key` is allowed under the specified
        rate limit.

        Args:
            key: Unique identifier for the rate limit (e.g., user ID, IP address)
            rate: Rate limit string (e.g., "100/minute", "1000/hour")
            algorithm: Algorithm to use (defaults to config.default_algorithm)
            tenant_type: Tenant type for multi-tenant setups (e.g., "free", "premium")
            cost: Cost of this request (default 1, can be higher for expensive operations)

        Returns:
            True if request is allowed

        Raises:
            RateLimitExceeded: If rate limit is exceeded
            RateLimitConfigError: If configuration is invalid
            BackendError: If backend operation fails

        Examples:
            >>> # Simple check
            >>> await limiter.check(key="user:123", rate="100/minute")
            True

            >>> # Multi-tenant check
            >>> await limiter.check(
            ...     key="api:key:abc123",
            ...     rate="1000/hour",
            ...     tenant_type="premium"
            ... )
            True

            >>> # Higher cost operation
            >>> await limiter.check(
            ...     key="user:123",
            ...     rate="100/minute",
            ...     cost=10  # This request counts as 10 regular requests
            ... )
            True
        """
        # Ensure we're connected
        if not self._connected:
            await self.connect()

        # Parse rate limit
        try:
            requests, window_seconds = parse_rate(rate)
        except ValueError as e:
            raise RateLimitConfigError(f"Invalid rate format: {e}")

        # Select algorithm
        algorithm = algorithm or self.config.default_algorithm
        if algorithm not in ["fixed_window", "token_bucket"]:
            raise RateLimitConfigError(f"Unknown algorithm: {algorithm}")

        # Generate time-based key for fixed window
        time_window = get_time_window(window_seconds)
        tenant_type = tenant_type or "default"

        # Generate Redis key
        full_key = generate_key(
            self.config.key_prefix,
            key,
            tenant_type,
            time_window,
        )

        # Use integer math (multiply by 1000 for precision)
        max_requests = requests * 1000
        cost_with_multiplier = cost * 1000

        # For now, only fixed window is implemented
        if algorithm == "fixed_window":
            result = await self.backend.check_fixed_window(
                full_key, max_requests, window_seconds
            )
        else:
            raise NotImplementedError(f"Algorithm {algorithm} not yet implemented")

        # Check if request is allowed
        if not result.allowed:
            # Convert milliseconds to seconds for retry_after
            retry_after_seconds = max(1, result.retry_after // 1000)
            remaining_requests = result.remaining // 1000

            raise RateLimitExceeded(
                retry_after=retry_after_seconds,
                limit=rate,
                remaining=remaining_requests,
            )

        logger.debug(
            f"Rate limit check passed for key={key}, "
            f"remaining={result.remaining // 1000}"
        )

        return True

    def limit(
        self,
        rate: str,
        key: Optional[Callable] = None,
        tenant_type: Optional[Callable] = None,
        algorithm: Optional[str] = None,
        cost: Optional[Callable] = None,
    ):
        """
        Create a decorator for rate limiting endpoints.

        This method returns a decorator that can be used to rate limit
        FastAPI or other async endpoints.

        Args:
            rate: Rate limit string (e.g., "100/minute")
            key: Optional function to extract key from request
                 If not provided, uses request.client.host (IP address)
            tenant_type: Optional function to extract tenant type from request
            algorithm: Algorithm to use (defaults to config.default_algorithm)
            cost: Optional function to calculate request cost

        Returns:
            Decorator function for rate limiting

        Examples:
            >>> @app.get("/api/data")
            >>> @limiter.limit("100/minute")
            >>> async def get_data(request: Request):
            >>>     return {"data": "..."}

            >>> @app.get("/api/users/{user_id}")
            >>> @limiter.limit(
            ...     "1000/hour",
            ...     key=lambda req: req.path_params.get("user_id")
            ... )
            >>> async def get_user(request: Request, user_id: str):
            >>>     return {"user_id": user_id}

            >>> @app.post("/api/expensive")
            >>> @limiter.limit(
            ...     "100/minute",
            ...     cost=lambda req: 10 if req.headers.get("X-Premium") else 1
            ... )
            >>> async def expensive_operation(request: Request):
            >>>     return {"status": "completed"}
        """
        from .decorators import create_limit_decorator

        return create_limit_decorator(
            limiter=self,
            rate=rate,
            key_func=key,
            tenant_func=tenant_type,
            algorithm=algorithm,
            cost_func=cost,
        )

    async def reset(self, key: str, tenant_type: Optional[str] = None) -> bool:
        """
        Reset rate limit for a specific key.

        This method removes all rate limit data for the specified key,
        allowing it to start fresh.

        Args:
            key: Unique identifier for the rate limit
            tenant_type: Tenant type (defaults to "default")

        Returns:
            True if reset was successful, False if key didn't exist

        Examples:
            >>> await limiter.reset("user:123")
            True
        """
        if not self._connected:
            await self.connect()

        # For fixed window, we need to generate the current window key
        # We'll reset all common window sizes
        tenant_type = tenant_type or "default"
        reset_success = False

        for window_seconds in [1, 60, 3600, 86400]:  # second, minute, hour, day
            time_window = get_time_window(window_seconds)
            full_key = generate_key(
                self.config.key_prefix,
                key,
                tenant_type,
                time_window,
            )
            result = await self.backend.reset(full_key)
            if result:
                reset_success = True

        return reset_success

    async def get_usage(
        self, key: str, rate: str, tenant_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get current usage statistics for a key.

        Args:
            key: Unique identifier for the rate limit
            rate: Rate limit string to determine window
            tenant_type: Tenant type (defaults to "default")

        Returns:
            Dictionary with usage statistics

        Examples:
            >>> usage = await limiter.get_usage("user:123", "100/minute")
            >>> print(usage)
            {'current': 42, 'limit': 100, 'remaining': 58, 'ttl': 45}
        """
        if not self._connected:
            await self.connect()

        requests, window_seconds = parse_rate(rate)
        time_window = get_time_window(window_seconds)
        tenant_type = tenant_type or "default"

        full_key = generate_key(
            self.config.key_prefix,
            key,
            tenant_type,
            time_window,
        )

        usage = await self.backend.get_usage(full_key)

        # Convert from integer math
        current_requests = usage["current"] // 1000 if usage["current"] > 0 else 0
        remaining = max(0, requests - current_requests)

        return {
            "current": current_requests,
            "limit": requests,
            "remaining": remaining,
            "ttl": usage["ttl"],
            "window_seconds": window_seconds,
        }

    async def health_check(self) -> bool:
        """
        Check if the rate limiter is healthy.

        Returns:
            True if healthy, False otherwise

        Examples:
            >>> if await limiter.health_check():
            >>>     print("Rate limiter is healthy")
        """
        if not self._connected:
            return False

        return await self.backend.health_check()
