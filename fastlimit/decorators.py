"""
Decorator implementations for rate limiting.
"""

import functools
from typing import Callable, Optional, Any
from inspect import iscoroutinefunction
import logging

from .exceptions import RateLimitExceeded

logger = logging.getLogger(__name__)


def create_limit_decorator(
    limiter: Any,
    rate: str,
    key_func: Optional[Callable] = None,
    tenant_func: Optional[Callable] = None,
    algorithm: Optional[str] = None,
    cost_func: Optional[Callable] = None,
):
    """
    Create a rate limit decorator for async functions.

    This function creates a decorator that can be used to rate limit
    FastAPI endpoints or any async function that receives a request-like
    object as its first argument.

    Args:
        limiter: RateLimiter instance
        rate: Rate limit string (e.g., "100/minute")
        key_func: Optional function to extract rate limit key from request
        tenant_func: Optional function to extract tenant type from request
        algorithm: Algorithm to use for rate limiting
        cost_func: Optional function to calculate request cost

    Returns:
        Decorator function

    Examples:
        >>> limiter = RateLimiter()
        >>> decorator = create_limit_decorator(
        ...     limiter=limiter,
        ...     rate="100/minute",
        ...     key_func=lambda req: req.client.host
        ... )
        >>> @decorator
        >>> async def my_endpoint(request):
        >>>     return {"status": "ok"}
    """

    def decorator(func: Callable) -> Callable:
        """The actual decorator."""

        if not iscoroutinefunction(func):
            # For sync functions, create an async wrapper
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                # Extract request from arguments
                request = _extract_request(args, kwargs)

                # Perform rate limit check
                await _check_rate_limit(
                    limiter=limiter,
                    request=request,
                    rate=rate,
                    key_func=key_func,
                    tenant_func=tenant_func,
                    algorithm=algorithm,
                    cost_func=cost_func,
                )

                # Call the original function
                # Note: This converts sync to async, which may not be ideal
                return func(*args, **kwargs)

            return async_wrapper
        else:
            # For async functions
            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                # Extract request from arguments
                request = _extract_request(args, kwargs)

                # Perform rate limit check
                await _check_rate_limit(
                    limiter=limiter,
                    request=request,
                    rate=rate,
                    key_func=key_func,
                    tenant_func=tenant_func,
                    algorithm=algorithm,
                    cost_func=cost_func,
                )

                # Call the original async function
                return await func(*args, **kwargs)

            return async_wrapper

    return decorator


def _extract_request(args: tuple, kwargs: dict) -> Any:
    """
    Extract request object from function arguments.

    FastAPI passes the Request object as the first positional argument
    or as a keyword argument named 'request'.

    Args:
        args: Positional arguments
        kwargs: Keyword arguments

    Returns:
        Request object or None

    Raises:
        ValueError: If request cannot be found
    """
    # Try to get from kwargs first (more explicit)
    if "request" in kwargs:
        return kwargs["request"]

    # Try first positional argument
    if args:
        # Check if it looks like a request object
        # (has client attribute for FastAPI Request)
        first_arg = args[0]
        if hasattr(first_arg, "client") or hasattr(first_arg, "headers"):
            return first_arg

    # If we can't find a request, raise an error
    raise ValueError(
        "Could not extract request object from function arguments. "
        "Ensure the decorated function receives a Request object."
    )


async def _check_rate_limit(
    limiter: Any,
    request: Any,
    rate: str,
    key_func: Optional[Callable],
    tenant_func: Optional[Callable],
    algorithm: Optional[str],
    cost_func: Optional[Callable],
) -> None:
    """
    Perform rate limit check and handle the result.

    Args:
        limiter: RateLimiter instance
        request: Request object
        rate: Rate limit string
        key_func: Function to extract key
        tenant_func: Function to extract tenant type
        algorithm: Algorithm to use
        cost_func: Function to calculate cost

    Raises:
        RateLimitExceeded: If rate limit is exceeded
    """
    # Extract rate limit key
    if key_func:
        try:
            key = key_func(request)
        except Exception as e:
            logger.error(f"Error extracting key with key_func: {e}")
            key = _get_default_key(request)
    else:
        key = _get_default_key(request)

    # Extract tenant type
    tenant_type = None
    if tenant_func:
        try:
            tenant_type = tenant_func(request)
        except Exception as e:
            logger.error(f"Error extracting tenant type: {e}")

    # Calculate cost
    cost = 1
    if cost_func:
        try:
            cost = cost_func(request)
        except Exception as e:
            logger.error(f"Error calculating cost: {e}")
            cost = 1

    # Perform rate limit check
    try:
        await limiter.check(
            key=key,
            rate=rate,
            algorithm=algorithm,
            tenant_type=tenant_type,
            cost=cost,
        )
    except RateLimitExceeded as e:
        # Add rate limit headers to the request state
        # This allows middleware to add them to the response
        if hasattr(request, "state"):
            request.state.rate_limit_headers = {
                "X-RateLimit-Limit": rate,
                "X-RateLimit-Remaining": str(e.remaining),
                "X-RateLimit-Reset": str(e.retry_after),
                "Retry-After": str(e.retry_after),
            }

        # Re-raise the exception
        raise


def _get_default_key(request: Any) -> str:
    """
    Get default rate limit key from request.

    Default behavior is to use the client's IP address.

    Args:
        request: Request object

    Returns:
        Rate limit key
    """
    # Try to get IP address from various sources
    # FastAPI/Starlette Request
    if hasattr(request, "client") and request.client:
        return f"ip:{request.client.host}"

    # Check headers for forwarded IP
    if hasattr(request, "headers"):
        # X-Forwarded-For header (behind proxy)
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # Take the first IP in the chain
            ip = forwarded_for.split(",")[0].strip()
            return f"ip:{ip}"

        # X-Real-IP header (nginx)
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return f"ip:{real_ip}"

    # Fallback to a generic key
    logger.warning("Could not determine client IP, using fallback key")
    return "ip:unknown"


class RateLimitMiddleware:
    """
    ASGI middleware for rate limiting.

    This middleware can be added to FastAPI or other ASGI applications
    to automatically handle rate limiting for all endpoints.

    Examples:
        >>> from fastapi import FastAPI
        >>> from fastlimit import RateLimiter, RateLimitMiddleware
        >>>
        >>> app = FastAPI()
        >>> limiter = RateLimiter()
        >>>
        >>> app.add_middleware(
        ...     RateLimitMiddleware,
        ...     limiter=limiter,
        ...     default_rate="1000/minute"
        ... )
    """

    def __init__(
        self,
        app: Any,
        limiter: Any,
        default_rate: str = "1000/minute",
        exclude_paths: Optional[list] = None,
    ):
        """
        Initialize middleware.

        Args:
            app: ASGI application
            limiter: RateLimiter instance
            default_rate: Default rate limit for all endpoints
            exclude_paths: List of paths to exclude from rate limiting
        """
        self.app = app
        self.limiter = limiter
        self.default_rate = default_rate
        self.exclude_paths = exclude_paths or []

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        """
        ASGI middleware implementation.

        Args:
            scope: ASGI scope
            receive: ASGI receive callable
            send: ASGI send callable
        """
        # Only process HTTP requests
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Check if path is excluded
        path = scope.get("path", "")
        if any(path.startswith(excluded) for excluded in self.exclude_paths):
            await self.app(scope, receive, send)
            return

        # Create a simple request-like object for rate limiting
        class SimpleRequest:
            def __init__(self, scope):
                self.client = type("Client", (), {
                    "host": scope.get("client", ["unknown", None])[0]
                })()
                self.headers = dict(scope.get("headers", []))
                self.path = scope.get("path", "")

        request = SimpleRequest(scope)

        # Check rate limit
        try:
            await self.limiter.check(
                key=_get_default_key(request),
                rate=self.default_rate,
            )
        except RateLimitExceeded as e:
            # Send 429 response
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(e.retry_after).encode()),
                    (b"x-ratelimit-limit", self.default_rate.encode()),
                    (b"x-ratelimit-remaining", str(e.remaining).encode()),
                ],
            })
            await send({
                "type": "http.response.body",
                "body": f'{{"error": "Rate limit exceeded", "retry_after": {e.retry_after}}}'.encode(),
            })
            return

        # Continue with the application
        await self.app(scope, receive, send)
