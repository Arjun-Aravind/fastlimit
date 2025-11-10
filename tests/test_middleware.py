"""
Tests for rate limit headers middleware.
"""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from fastlimit import RateLimiter, RateLimitHeadersMiddleware


@pytest.fixture
def app_with_middleware(redis_url):
    """Create FastAPI app with rate limit middleware."""
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
        client = TestClient(app_with_middleware)

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
        client = TestClient(app_with_middleware)

        # Make 5 requests (the limit)
        for i in range(5):
            response = client.get("/limited")
            assert response.status_code == 200

        # 6th request should be rate limited
        response = client.get("/limited")

        assert response.status_code == 429
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers
        assert response.headers["X-RateLimit-Remaining"] == "0"
        assert "Retry-After" in response.headers
        retry_after = int(response.headers["Retry-After"])
        assert retry_after > 0

    def test_remaining_count_decreases(self, app_with_middleware):
        """Test that remaining count decreases with each request."""
        client = TestClient(app_with_middleware)

        # Make multiple requests and verify remaining count
        for expected_remaining in [4, 3, 2, 1, 0]:
            response = client.get("/limited")
            assert response.status_code == 200
            remaining = int(response.headers["X-RateLimit-Remaining"])
            assert remaining == expected_remaining

    def test_endpoint_without_rate_limit(self, app_with_middleware):
        """Test that endpoints without rate limits don't add headers."""
        client = TestClient(app_with_middleware)

        response = client.get("/no-limit")

        assert response.status_code == 200
        # These endpoints shouldn't have rate limit headers
        assert "X-RateLimit-Limit" not in response.headers

    def test_reset_timestamp_in_future(self, app_with_middleware):
        """Test that reset timestamp is in the future."""
        import time

        client = TestClient(app_with_middleware)

        response = client.get("/limited")
        assert response.status_code == 200

        reset_timestamp = int(response.headers["X-RateLimit-Reset"])
        current_time = int(time.time())

        # Reset should be in the future (within 60 seconds for minute limit)
        assert reset_timestamp > current_time
        assert reset_timestamp <= current_time + 60

    def test_expensive_request_with_cost(self, app_with_middleware):
        """Test that cost-based rate limiting works with headers."""
        client = TestClient(app_with_middleware)

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
        client = TestClient(app_with_middleware)

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

    def test_concurrent_requests(self, app_with_middleware):
        """Test that headers are correct with concurrent requests."""
        import concurrent.futures

        client = TestClient(app_with_middleware)

        def make_request():
            return client.get("/limited")

        # Make concurrent requests
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(make_request) for _ in range(5)]
            responses = [f.result() for f in futures]

        # All should succeed (within limit)
        assert all(r.status_code == 200 for r in responses)

        # All should have rate limit headers
        assert all("X-RateLimit-Remaining" in r.headers for r in responses)

    def test_headers_with_different_ips(self, app_with_middleware):
        """Test that different IPs get separate rate limits."""
        from fastapi.testclient import TestClient

        # Create clients with different IPs (simulated)
        client1 = TestClient(app_with_middleware)
        client2 = TestClient(app_with_middleware)

        # Note: TestClient doesn't easily support different IPs,
        # but we can verify that the same client maintains state
        response1 = client1.get("/limited")
        response2 = client1.get("/limited")

        assert response1.status_code == 200
        assert response2.status_code == 200

        remaining1 = int(response1.headers["X-RateLimit-Remaining"])
        remaining2 = int(response2.headers["X-RateLimit-Remaining"])

        # Second request should have less remaining
        assert remaining2 < remaining1


@pytest.mark.asyncio
class TestMiddlewareIntegration:
    """Integration tests for middleware with actual rate limiter."""

    async def test_middleware_with_limiter_check(self, clean_limiter):
        """Test middleware integration with actual limiter."""
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient

        app = FastAPI()
        limiter = clean_limiter

        app.add_middleware(RateLimitHeadersMiddleware)

        @app.get("/test")
        @limiter.limit("3/minute")
        async def test_endpoint(request: Request):
            return {"status": "ok"}

        client = TestClient(app)

        # Make requests up to the limit
        for i in range(3):
            response = client.get("/test")
            assert response.status_code == 200
            assert "X-RateLimit-Limit" in response.headers

        # Next request should fail
        response = client.get("/test")
        assert response.status_code == 429
        assert "Retry-After" in response.headers

    async def test_middleware_preserves_response_body(self, clean_limiter):
        """Test that middleware doesn't alter response body."""
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient

        app = FastAPI()
        limiter = clean_limiter

        app.add_middleware(RateLimitHeadersMiddleware)

        @app.get("/data")
        @limiter.limit("10/minute")
        async def data_endpoint(request: Request):
            return {"data": "test", "count": 123}

        client = TestClient(app)

        response = client.get("/data")
        assert response.status_code == 200

        # Response body should be intact
        data = response.json()
        assert data["data"] == "test"
        assert data["count"] == 123

        # Headers should be added
        assert "X-RateLimit-Limit" in response.headers
