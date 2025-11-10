"""
FastLimit - Production-ready rate limiting library for Python
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A high-performance, Redis-backed rate limiting library with async support.

Basic usage:
    >>> from fastlimit import RateLimiter
    >>> limiter = RateLimiter(redis_url="redis://localhost:6379")
    >>> await limiter.connect()
    >>> await limiter.check(key="user:123", rate="100/minute")

FastAPI integration:
    >>> from fastapi import FastAPI, Request
    >>> from fastlimit import RateLimiter
    >>>
    >>> app = FastAPI()
    >>> limiter = RateLimiter()
    >>>
    >>> @app.get("/api/data")
    >>> @limiter.limit("100/minute")
    >>> async def get_data(request: Request):
    >>>     return {"data": "..."}
"""

from .limiter import RateLimiter
from .exceptions import RateLimitExceeded, RateLimitConfigError, BackendError
from .models import RateLimitConfig
from .middleware import RateLimitHeadersMiddleware

__version__ = "0.1.0"
__author__ = "Arjun"
__email__ = "arjun@example.com"

__all__ = [
    "RateLimiter",
    "RateLimitExceeded",
    "RateLimitConfigError",
    "BackendError",
    "RateLimitConfig",
    "RateLimitHeadersMiddleware",
]
