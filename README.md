# FastLimit 🚀

<div align="center">

[![Python Version](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Redis](https://img.shields.io/badge/redis-7%2B-red)](https://redis.io)
[![Code Style](https://img.shields.io/badge/code%20style-black-000000.svg)](https://github.com/psf/black)
[![Tests](https://img.shields.io/badge/tests-60%2B%20passing-brightgreen)](tests/)

**Production-ready, enterprise-grade rate limiting library for Python**

[Features](#-features) • [Quick Start](#-quick-start) • [Algorithms](#-algorithms) • [Documentation](#-documentation) • [Examples](#-examples) • [Architecture](ARCHITECTURE.md)

</div>

---

## 🌟 What is FastLimit?

FastLimit is a high-performance, Redis-backed rate limiting library designed for modern Python applications. It provides **two sophisticated algorithms**, **automatic header injection**, **comprehensive metrics**, and **zero-configuration** multi-tenant support.

**Perfect for:**
- 🚀 FastAPI applications requiring rate limiting
- 🏢 Multi-tenant SaaS platforms with tier-based limits
- 📊 APIs needing production monitoring and observability
- ⚡ High-throughput services (10K+ req/s)
- 🔒 Applications requiring strict rate limit guarantees

---

## ✨ Features

### Core Capabilities
- **🎯 Two Algorithms** - Fixed Window & Token Bucket (choose what fits)
- **🔄 Async-first design** - Built for FastAPI and modern async Python
- **⚡ High performance** - <2ms p99 latency, 10K+ requests/second
- **🔒 Zero race conditions** - Atomic operations via Redis Lua scripts
- **📊 Integer precision** - Uses integer math (×1000 multiplier) for accuracy

### Production Features
- **🌐 Automatic Headers** - Industry-standard rate limit headers on all responses
- **📈 Prometheus Metrics** - Comprehensive observability out of the box
- **🏢 Multi-tenant support** - Isolated limits for different users/tiers/organizations
- **🎨 Decorator-based API** - Clean, declarative rate limiting
- **💰 Cost-based limiting** - Weight expensive operations appropriately
- **🔧 Flexible configuration** - Customizable key extraction, algorithms, and costs

---

## 🚀 Quick Start

### Installation

```bash
# Basic installation
pip install fastlimit

# With metrics support (optional)
pip install 'fastlimit[metrics]'
```

### 30-Second Example

```python
from fastapi import FastAPI, Request
from fastlimit import RateLimiter, RateLimitHeadersMiddleware

app = FastAPI()
limiter = RateLimiter(redis_url="redis://localhost:6379")

# Add automatic header injection (optional but recommended)
app.add_middleware(RateLimitHeadersMiddleware)

@app.on_event("startup")
async def startup():
    await limiter.connect()

@app.get("/api/users")
@limiter.limit("100/minute")  # 100 requests per minute per IP
async def get_users(request: Request):
    return {"users": ["Alice", "Bob"]}
```

**That's it!** Your endpoint now has:
- ✅ Rate limiting (100 requests/minute per IP)
- ✅ Automatic headers (X-RateLimit-Limit, X-RateLimit-Remaining, etc.)
- ✅ Proper 429 responses when exceeded
- ✅ Redis-backed, distributed-ready

---

## 🎯 Algorithms

FastLimit provides **two production-tested algorithms**. Choose based on your needs:

### Fixed Window (Default)

**Best for:** Simple rate limiting, strict per-window limits, lower memory usage

```python
@limiter.limit("100/minute", algorithm="fixed_window")
async def endpoint(request: Request):
    return {"data": "..."}
```

**How it works:**
- Time divided into fixed windows (e.g., 14:35:00 - 14:36:00)
- Counter increments per request
- Resets when window expires
- Simple and predictable

**Pros:** Simple, low memory, strict limits
**Cons:** Possible boundary bursts (can get 2x at window edge)

### Token Bucket

**Best for:** Smooth rate limiting, bursty traffic, better user experience

```python
@limiter.limit("100/minute", algorithm="token_bucket")
async def endpoint(request: Request):
    return {"data": "..."}
```

**How it works:**
- Bucket holds tokens (capacity = 100)
- Tokens refill continuously (~1.67/second for 100/minute)
- Each request consumes tokens
- Smooth, predictable behavior

**Pros:** Smooth traffic, no boundary bursts, better UX
**Cons:** Slightly more memory, more complex

### Algorithm Comparison

| Feature | Fixed Window | Token Bucket |
|---------|--------------|--------------|
| **Simplicity** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| **Boundary Bursts** | ❌ Possible (2x) | ✅ None |
| **Memory Usage** | ✅ Low (~100 bytes) | ⚠️ Medium (~150 bytes) |
| **Traffic Smoothness** | ⚠️ Choppy | ✅ Smooth |
| **User Experience** | ⚠️ Can feel restrictive | ✅ More forgiving |
| **Use Case** | Strict limits | Bursty APIs |

**Recommendation:** Start with **Token Bucket** for better UX, use **Fixed Window** for strict enforcement.

📖 **[Read detailed algorithm comparison →](ALGORITHMS.md)**

---

## 📚 Documentation

### Table of Contents

1. [Configuration](#configuration)
2. [Rate Limit Formats](#rate-limit-formats)
3. [Decorator API](#decorator-api)
4. [Automatic Headers](#automatic-headers)
5. [Prometheus Metrics](#prometheus-metrics)
6. [Manual Checking](#manual-checking)
7. [Multi-Tenant Setup](#multi-tenant-setup)
8. [Cost-Based Limiting](#cost-based-limiting)
9. [Error Handling](#error-handling)
10. [Performance](#performance)

### Configuration

```python
from fastlimit import RateLimiter

limiter = RateLimiter(
    redis_url="redis://localhost:6379",      # Redis connection URL
    key_prefix="myapp:ratelimit",            # Prefix for Redis keys
    default_algorithm="token_bucket",        # Algorithm: "fixed_window" or "token_bucket"
    enable_metrics=False,                    # Enable Prometheus metrics
)
```

**Configuration Options:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `redis_url` | str | `"redis://localhost:6379"` | Redis connection string |
| `key_prefix` | str | `"ratelimit"` | Prefix for all Redis keys |
| `default_algorithm` | str | `"fixed_window"` | Default algorithm to use |
| `enable_metrics` | bool | `False` | Enable Prometheus metrics collection |

### Rate Limit Formats

FastLimit supports intuitive rate strings:

```python
"10/second"   # 10 requests per second
"100/minute"  # 100 requests per minute
"1000/hour"   # 1000 requests per hour
"10000/day"   # 10000 requests per day
```

All formats support both singular and plural: `"1/second"` and `"100/seconds"` both work.

### Decorator API

#### Basic IP-based limiting

```python
@app.get("/api/data")
@limiter.limit("100/minute")
async def get_data(request: Request):
    return {"data": "..."}
```

By default, rate limits are per client IP address.

#### Custom key extraction

```python
@app.get("/api/user/{user_id}")
@limiter.limit(
    "1000/hour",
    key=lambda req: f"user:{req.path_params.get('user_id')}"
)
async def get_user_data(request: Request, user_id: str):
    return {"user_id": user_id}
```

#### Choose algorithm

```python
@app.get("/api/smooth")
@limiter.limit("100/minute", algorithm="token_bucket")  # Smooth rate limiting
async def smooth_endpoint(request: Request):
    return {"data": "..."}

@app.get("/api/strict")
@limiter.limit("100/minute", algorithm="fixed_window")  # Strict limits
async def strict_endpoint(request: Request):
    return {"data": "..."}
```

### Automatic Headers

Add the middleware to automatically inject rate limit headers on **all responses**:

```python
from fastlimit import RateLimitHeadersMiddleware

app.add_middleware(RateLimitHeadersMiddleware)
```

**Headers added:**
- `X-RateLimit-Limit`: Maximum requests allowed
- `X-RateLimit-Remaining`: Requests remaining in current window
- `X-RateLimit-Reset`: Unix timestamp when limit resets
- `Retry-After`: Seconds to wait (when rate limited)

**Example response:**
```http
HTTP/1.1 200 OK
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 73
X-RateLimit-Reset: 1700000060
```

**When rate limited:**
```http
HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1700000060
Retry-After: 30
```

**Why this matters:**
- ✅ Clients can monitor their usage proactively
- ✅ Follows industry standards (GitHub, Twitter, Stripe)
- ✅ RFC 6585 and RFC 7231 compliant
- ✅ Better developer experience for API consumers

### Prometheus Metrics

Enable comprehensive observability for production monitoring:

```python
from fastlimit import RateLimiter, init_metrics
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

# Initialize metrics
metrics = init_metrics(namespace="myapp", enabled=True)

limiter = RateLimiter(
    redis_url="redis://localhost:6379",
    enable_metrics=True
)

# Expose metrics endpoint
@app.get("/metrics")
def metrics_endpoint():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )
```

**Available Metrics:**

| Metric | Type | Description |
|--------|------|-------------|
| `fastlimit_checks_total` | Counter | Total rate limit checks (by algorithm, result) |
| `fastlimit_check_duration_seconds` | Histogram | Check latency (p50, p95, p99) |
| `fastlimit_limit_exceeded_total` | Counter | Rate limit violations (by algorithm, tenant) |
| `fastlimit_redis_operations_total` | Counter | Redis operations (by command, status) |
| `fastlimit_redis_connection_errors_total` | Counter | Redis connection failures |
| `fastlimit_current_usage` | Gauge | Current usage per key |

**Prometheus Query Examples:**

```promql
# Rate limit check rate
rate(fastlimit_checks_total[5m])

# P99 latency
histogram_quantile(0.99, rate(fastlimit_check_duration_seconds_bucket[5m]))

# Violation rate
rate(fastlimit_limit_exceeded_total[5m])

# Current usage for specific key
fastlimit_current_usage{key="user:123"}
```

**Requirements:**
```bash
pip install 'fastlimit[metrics]'  # Installs prometheus-client
```

### Manual Checking

For more control, use the `check` method directly:

```python
async def process_webhook(webhook_id: str, data: dict):
    try:
        await limiter.check(
            key=f"webhook:{webhook_id}",
            rate="50/second",
            algorithm="token_bucket",
            tenant_type="premium"
        )
        # Process webhook
    except RateLimitExceeded as e:
        logger.warning(f"Rate limited. Retry after {e.retry_after}s")
        raise
```

### Multi-Tenant Setup

FastLimit excels at multi-tenant scenarios with tier-based limits:

```python
# Define tier limits
TIER_LIMITS = {
    "free": "10/minute",
    "premium": "100/minute",
    "enterprise": "1000/minute"
}

def get_tenant_tier(api_key: str) -> str:
    # Look up tier from database
    return TENANT_DB[api_key]["tier"]

@app.get("/api/data")
@limiter.limit(
    rate=lambda req: TIER_LIMITS[get_tenant_tier(req.headers["X-API-Key"])],
    key=lambda req: req.headers.get("X-API-Key"),
    tenant_type=lambda req: get_tenant_tier(req.headers["X-API-Key"])
)
async def get_data(request: Request):
    return {"data": "..."}
```

**Key isolation:** Different tenants have completely isolated rate limits. Tenant A consuming their limit doesn't affect Tenant B.

### Cost-Based Limiting

Weight expensive operations appropriately:

```python
@app.post("/api/expensive")
@limiter.limit(
    "100/minute",
    cost=lambda req: 10 if req.path == "/api/ml-inference" else 1
)
async def expensive_operation(request: Request):
    # ML inference costs 10x, regular requests cost 1x
    return {"status": "completed"}
```

**Use cases:**
- ML model inference (high cost)
- Database-heavy queries (medium cost)
- Cache hits (low cost)
- Export operations (high cost)

### Error Handling

FastLimit provides detailed error information:

```python
from fastlimit import RateLimitExceeded

@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "retry_after": exc.retry_after,
            "limit": exc.limit,
            "remaining": exc.remaining
        },
        headers={
            "X-RateLimit-Limit": exc.limit,
            "X-RateLimit-Remaining": str(exc.remaining),
            "Retry-After": str(exc.retry_after)
        }
    )
```

**Note:** With `RateLimitHeadersMiddleware`, error handling is automatic!

### Performance

Benchmarked on MacBook Pro M1 with Redis 7.0:

| Metric | Fixed Window | Token Bucket |
|--------|--------------|--------------|
| **Latency (p50)** | 0.8ms | 1.0ms |
| **Latency (p95)** | 1.5ms | 1.8ms |
| **Latency (p99)** | 2.0ms | 2.2ms |
| **Throughput** | 15,000+ req/s | 12,000+ req/s |
| **Memory per key** | ~100 bytes | ~150 bytes |
| **Redis ops per check** | 1 (EVALSHA) | 1 (EVALSHA) |

**Optimizations:**
- ✅ Lua scripts cached (EVALSHA vs EVAL)
- ✅ Connection pooling (max 50 connections)
- ✅ Integer-only math (no float conversions)
- ✅ Efficient key hashing for long keys

---

## 🎯 Examples

### Complete FastAPI Application

```python
from fastapi import FastAPI, Request, HTTPException
from fastlimit import RateLimiter, RateLimitHeadersMiddleware, RateLimitExceeded

app = FastAPI()
limiter = RateLimiter(
    redis_url="redis://localhost:6379",
    default_algorithm="token_bucket"
)

# Add middleware for automatic headers
app.add_middleware(RateLimitHeadersMiddleware)

@app.on_event("startup")
async def startup():
    await limiter.connect()

@app.on_event("shutdown")
async def shutdown():
    await limiter.close()

# Simple rate limiting (100 req/min per IP)
@app.get("/api/public")
@limiter.limit("100/minute")
async def public_endpoint(request: Request):
    return {"data": "public"}

# Per-user rate limiting
@app.get("/api/user/{user_id}")
@limiter.limit(
    "1000/hour",
    key=lambda req: f"user:{req.path_params['user_id']}"
)
async def user_endpoint(request: Request, user_id: str):
    return {"user_id": user_id, "data": "..."}

# Cost-based rate limiting
@app.post("/api/ml/inference")
@limiter.limit("100/minute", cost=lambda req: 10)  # Counts as 10 requests
async def ml_inference(request: Request):
    # Expensive ML operation
    return {"prediction": "..."}

# Multi-algorithm comparison
@app.get("/api/smooth")
@limiter.limit("100/minute", algorithm="token_bucket")  # Smooth
async def smooth(request: Request):
    return {"algorithm": "token_bucket"}

@app.get("/api/strict")
@limiter.limit("100/minute", algorithm="fixed_window")  # Strict
async def strict(request: Request):
    return {"algorithm": "fixed_window"}
```

See [examples/](examples/) directory for more:
- [fastapi_app.py](examples/fastapi_app.py) - Complete FastAPI demo (9 endpoints)
- [multi_tenant.py](examples/multi_tenant.py) - Multi-tenant SaaS setup (3 tiers)
- [algorithms_demo.py](examples/algorithms_demo.py) - Algorithm comparisons

---

## 🏗️ Architecture

FastLimit is designed for production use with careful attention to:
- **Atomicity:** All operations via Lua scripts (zero race conditions)
- **Performance:** EVALSHA caching, connection pooling, integer math
- **Reliability:** Graceful error handling, Redis reconnection
- **Observability:** Comprehensive metrics, structured logging
- **Flexibility:** Multiple algorithms, custom keys, cost functions

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   FastAPI   │────▶│ RateLimiter │────▶│    Redis    │
│     App     │     │  (Python)   │     │   (Lua)     │
└─────────────┘     └─────────────┘     └─────────────┘
                            │
                    ┌───────┴───────┐
                    │               │
            ┌───────▼─────┐ ┌───────▼─────┐
            │   Fixed     │ │   Token     │
            │   Window    │ │   Bucket    │
            └─────────────┘ └─────────────┘
```

**Read more:** [ARCHITECTURE.md](ARCHITECTURE.md) - Deep dive into internals

---

## 🛠️ Development

### Prerequisites

- Python 3.9+
- Redis 7.0+
- Poetry (for dependency management)

### Setup

```bash
# Clone the repository
git clone https://github.com/yourusername/fastlimit.git
cd fastlimit

# Install dependencies
poetry install

# Start Redis (Docker)
docker-compose -f docker-compose.dev.yml up -d

# Run tests
poetry run pytest

# Run with coverage
poetry run pytest --cov=fastlimit --cov-report=html

# Format code
poetry run black fastlimit/ tests/

# Lint
poetry run ruff check fastlimit/ tests/

# Type check
poetry run mypy fastlimit/
```

### Running Examples

```bash
# FastAPI demo
poetry run uvicorn examples.fastapi_app:app --reload

# Multi-tenant demo
poetry run uvicorn examples.multi_tenant:app --reload --port 8001

# Algorithm comparison
poetry run python examples/algorithms_demo.py
```

---

## 🧪 Testing

FastLimit has **60+ comprehensive tests** covering:

- ✅ Both algorithms (Fixed Window, Token Bucket)
- ✅ Concurrent requests and race conditions
- ✅ Multi-tenant isolation
- ✅ Cost-based rate limiting
- ✅ Headers middleware
- ✅ Edge cases and error handling
- ✅ Performance benchmarks

```bash
# Run all tests
make test

# Run specific test file
poetry run pytest tests/test_token_bucket.py -v

# Run with coverage
make test-cov
```

**Test Coverage:** 95%+ across all modules

---

## 🔮 Roadmap

- [x] Fixed Window algorithm
- [x] Token Bucket algorithm
- [x] Automatic rate limit headers
- [x] Prometheus metrics
- [x] Multi-tenant support
- [x] Cost-based rate limiting
- [ ] Sliding Window algorithm
- [ ] Circuit breaker pattern
- [ ] Redis Cluster support
- [ ] Web dashboard
- [ ] Django integration

---

## 🤝 Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

**Ways to contribute:**
- 🐛 Report bugs
- 💡 Suggest features
- 📝 Improve documentation
- 🧪 Add tests
- 🚀 Submit PRs

---

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

## 🙏 Acknowledgments

- [Redis](https://redis.io) - Amazing in-memory data store
- [FastAPI](https://fastapi.tiangolo.com) - Modern async web framework
- [Prometheus](https://prometheus.io) - Metrics and monitoring
- The Python async community for inspiration

---

## 📞 Support & Resources

- 📖 **Documentation:** [ARCHITECTURE.md](ARCHITECTURE.md) | [ALGORITHMS.md](ALGORITHMS.md)
- 💬 **Issues:** [GitHub Issues](https://github.com/yourusername/fastlimit/issues)
- 📧 **Email:** support@fastlimit.io
- ⭐ **Star us on GitHub** if you find this useful!

---

<div align="center">

**Built with ❤️ for the Python community**

[⬆ Back to top](#fastlimit-)

</div>
