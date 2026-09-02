"""
Tests for rate limit headers middleware.
"""

import asyncio

import pytest
import redis as sync_redis
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from fastlimit import RateLimiter, RateLimitHeadersMiddleware


@pytest.fixture
def app_with_middleware(redis_url):
    """Create FastAPI app with rate limit middleware."""
    # Flush Redis so each test starts with clean rate limit state.
    # Without this, the fixed key_prefix + shared IP key means tests
    # consume each other's quotas and fail in an order-dependent way.
    client = sync_redis.from_url(redis_url)
    client.flushdb()
    client.close()

    app = FastAPI()
    limiter = RateLimiter(redis_url=redis_url, key_prefix="test:middleware")

    # Add middleware
    app.add_middleware(RateLimitHeadersMiddleware)

    @app.on_event("startup")
    async def startup():
        await limiter.connect()

    @app.on_event("shutdown")
    async def shutdown():
        await limiter.close()

    @app.get("/limited")
    @limiter.limit("5/minute")
    async def limited_endpoint(request: Request):
        return {"message": "success"}

    @app.get("/no-limit")
    async def no_limit_endpoint(request: Request):
        return {"message": "no limit"}

    @app.get("/expensive")
    @limiter.limit("10/minute", cost=lambda req: 5)
    async def expensive_endpoint(request: Request):
        return {"message": "expensive"}

    app.state.limiter = limiter
    return app


class TestRateLimitHeadersMiddleware:
    """Test suite for rate limit headers middleware."""

    def test_successful_request_has_headers(self, app_with_middleware):
        """Test that successful requests include rate limit headers."""
        # Enter the client context so all requests share one event loop.
        # Without it, starlette spins up a new loop per request and the
        # Redis connection pool is left bound to a closed loop.
        with TestClient(app_with_middleware) as client:
            response = client.get("/limited")

            assert response.status_code == 200
            # Check that rate limit headers are present
            assert "X-RateLimit-Limit" in response.headers
            assert "X-RateLimit-Remaining" in response.headers
            assert "X-RateLimit-Reset" in response.headers

            # Verify header values
            assert response.headers["X-RateLimit-Limit"] == "5"
            remaining = int(response.headers["X-RateLimit-Remaining"])
            assert 0 <= remaining <= 5

    def test_rate_limited_request_has_retry_after(self, app_with_middleware):
        """Test that rate limited requests include Retry-After header."""
        import time

        with TestClient(app_with_middleware) as client:
            # Make 5 requests (the limit)
            for _ in range(5):
                response = client.get("/limited")
                assert response.status_code == 200

            # 6th request should be rate limited
            before = int(time.time())
            response = client.get("/limited")

            assert response.status_code == 429
            assert "X-RateLimit-Limit" in response.headers
            assert (
                response.headers["X-RateLimit-Limit"] == "5"
            )  # numeric limit, not "5/minute"
            assert "X-RateLimit-Remaining" in response.headers
            assert response.headers["X-RateLimit-Remaining"] == "0"
            assert "Retry-After" in response.headers
            retry_after = int(response.headers["Retry-After"])
            assert retry_after > 0

            # X-RateLimit-Reset must be a Unix timestamp, not a relative offset
            reset = int(response.headers["X-RateLimit-Reset"])
            assert reset > before, "Reset should be an epoch timestamp in the future"

    def test_remaining_count_decreases(self, app_with_middleware):
        """Test that remaining count decreases with each request."""
        with TestClient(app_with_middleware) as client:
            # Make multiple requests and verify remaining count
            for expected_remaining in [4, 3, 2, 1, 0]:
                response = client.get("/limited")
                assert response.status_code == 200
                remaining = int(response.headers["X-RateLimit-Remaining"])
                assert remaining == expected_remaining

    def test_endpoint_without_rate_limit(self, app_with_middleware):
        """Test that endpoints without rate limits don't add headers."""
        with TestClient(app_with_middleware) as client:
            response = client.get("/no-limit")

            assert response.status_code == 200
            # These endpoints shouldn't have rate limit headers
            assert "X-RateLimit-Limit" not in response.headers

    def test_reset_timestamp_in_future(self, app_with_middleware):
        """Test that reset timestamp is in the future."""
        import time

        with TestClient(app_with_middleware) as client:
            response = client.get("/limited")
            assert response.status_code == 200

            reset_timestamp = int(response.headers["X-RateLimit-Reset"])
            current_time = int(time.time())

            # Reset should be in the future (within 60 seconds for minute limit)
            assert reset_timestamp > current_time
            assert reset_timestamp <= current_time + 60

    def test_expensive_request_with_cost(self, app_with_middleware):
        """Test that cost-based rate limiting works with headers."""
        with TestClient(app_with_middleware) as client:
            # First request with cost=5 should use half the limit (10/minute, cost=5)
            response = client.get("/expensive")
            assert response.status_code == 200
            assert "X-RateLimit-Limit" in response.headers
            assert response.headers["X-RateLimit-Limit"] == "10"

            remaining = int(response.headers["X-RateLimit-Remaining"])
            # Should have 5 remaining (10 - 5)
            assert remaining == 5

            # Second request should use remaining 5
            response = client.get("/expensive")
            assert response.status_code == 200
            remaining = int(response.headers["X-RateLimit-Remaining"])
            assert remaining == 0

            # Third request should be rate limited
            response = client.get("/expensive")
            assert response.status_code == 429

    def test_rate_limit_error_response(self, app_with_middleware):
        """Test that rate limit error responses are properly formatted."""
        with TestClient(app_with_middleware) as client:
            # Exhaust the limit
            for _ in range(5):
                client.get("/limited")

            # Next request should return 429 with error details
            response = client.get("/limited")

            assert response.status_code == 429
            data = response.json()
            assert "error" in data
            assert "retry_after" in data
            assert data["error"] == "Rate limit exceeded"

    async def test_concurrent_requests(self, app_with_middleware):
        """Test that headers are correct with concurrent requests."""
        import httpx

        # Drive the ASGI app with a single event loop. TestClient called from
        # multiple threads runs each request in its own anyio portal event
        # loop, and the shared Redis client / asyncio.Lock are loop-bound,
        # which deadlocks the process at exit.
        transport = httpx.ASGITransport(app=app_with_middleware)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

                async def make_request():
                    return await client.get("/limited")

                responses = await asyncio.gather(*[make_request() for _ in range(5)])

            # All should succeed (within limit)
            assert all(r.status_code == 200 for r in responses)

            # All should have rate limit headers
            assert all("X-RateLimit-Remaining" in r.headers for r in responses)
        finally:
            # ASGITransport never runs lifespan events, so close the limiter
            # explicitly to release its Redis connection on this loop - even
            # when an assertion above fails.
            await app_with_middleware.state.limiter.close()

    def test_headers_with_different_ips(self, app_with_middleware):
        """Test that different IPs get separate rate limits."""
        # Note: TestClient doesn't easily support different IPs,
        # but we can verify that the same client maintains state
        with TestClient(app_with_middleware) as client1:
            response1 = client1.get("/limited")
            response2 = client1.get("/limited")

            assert response1.status_code == 200
            assert response2.status_code == 200

            remaining1 = int(response1.headers["X-RateLimit-Remaining"])
            remaining2 = int(response2.headers["X-RateLimit-Remaining"])

            # Second request should have less remaining
            assert remaining2 < remaining1


class TestMiddlewareIntegration:
    """Integration tests for middleware with actual rate limiter."""

    async def test_middleware_with_limiter_check(self, clean_limiter):
        """Test middleware integration with actual limiter."""
        import httpx
        from fastapi import FastAPI, Request

        app = FastAPI()
        limiter = clean_limiter

        app.add_middleware(RateLimitHeadersMiddleware)

        @app.get("/test")
        @limiter.limit("3/minute")
        async def test_endpoint(request: Request):
            return {"status": "ok"}

        # The limiter is connected on the test's event loop (async fixture),
        # so the app must be driven on that same loop - not in TestClient's portal.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Make requests up to the limit
            for _ in range(3):
                response = await client.get("/test")
                assert response.status_code == 200
                assert "X-RateLimit-Limit" in response.headers

            # Next request should fail
            response = await client.get("/test")
            assert response.status_code == 429
            assert "Retry-After" in response.headers

    async def test_middleware_preserves_response_body(self, clean_limiter):
        """Test that middleware doesn't alter response body."""
        import httpx
        from fastapi import FastAPI, Request

        app = FastAPI()
        limiter = clean_limiter

        app.add_middleware(RateLimitHeadersMiddleware)

        @app.get("/data")
        @limiter.limit("10/minute")
        async def data_endpoint(request: Request):
            return {"data": "test", "count": 123}

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/data")
            assert response.status_code == 200

            # Response body should be intact
            data = response.json()
            assert data["data"] == "test"
            assert data["count"] == 123

            # Headers should be added
            assert "X-RateLimit-Limit" in response.headers


class TestRateLimitMiddleware:
    """Test suite for the ASGI RateLimitMiddleware."""

    async def _drive(self, app, path="/anything", count=1):
        """Drive the ASGI app on this test's event loop via httpx."""
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            responses = []
            for _ in range(count):
                responses.append(await client.get(path))
            return responses[0] if count == 1 else responses

    @pytest.fixture
    def app_with_rate_limit_middleware(self, redis_url):
        """Create FastAPI app guarded by the ASGI RateLimitMiddleware."""
        from fastlimit.decorators import RateLimitMiddleware

        app = FastAPI()
        limiter = RateLimiter(redis_url=redis_url, key_prefix="test:asgi-middleware")

        app.add_middleware(
            RateLimitMiddleware,
            limiter=limiter,
            default_rate="5/minute",
        )

        @app.get("/anything")
        async def anything(request: Request):
            return {"message": "ok"}

        app.state.limiter = limiter
        return app

    async def test_429_response_has_standard_headers(
        self, app_with_rate_limit_middleware
    ):
        """Test that 429 responses include standard rate limit headers."""
        import time

        limiter = app_with_rate_limit_middleware.state.limiter
        await limiter.connect()
        try:
            # First 5 requests pass, 6th is rate limited
            for _ in range(5):
                response = await self._drive(app_with_rate_limit_middleware)
                assert response.status_code == 200

            before = int(time.time())
            response = await self._drive(app_with_rate_limit_middleware)

            assert response.status_code == 429
            assert response.headers["X-RateLimit-Limit"] == "5"
            assert response.headers["X-RateLimit-Remaining"] == "0"
            assert int(response.headers["Retry-After"]) > 0
            # Reset must be an epoch timestamp in the future, not a relative offset
            assert int(response.headers["X-RateLimit-Reset"]) > before
        finally:
            await limiter.close()

    async def test_excluded_paths_are_not_rate_limited(self, redis_url):
        """Test that exclude_paths requests bypass rate limiting."""
        from fastlimit.decorators import RateLimitMiddleware

        app = FastAPI()
        limiter = RateLimiter(redis_url=redis_url, key_prefix="test:asgi-exclude")

        app.add_middleware(
            RateLimitMiddleware,
            limiter=limiter,
            default_rate="1/minute",
            exclude_paths=["/health"],
        )

        @app.get("/health")
        async def health(request: Request):
            return {"status": "ok"}

        @app.get("/limited")
        async def limited(request: Request):
            return {"status": "ok"}

        await limiter.connect()
        try:
            # Excluded path is never rate limited
            for _ in range(5):
                assert (await self._drive(app, path="/health")).status_code == 200

            # Non-excluded path is limited
            assert (await self._drive(app, path="/limited")).status_code == 200
            assert (await self._drive(app, path="/limited")).status_code == 429
        finally:
            await limiter.close()
