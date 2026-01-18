# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2025-01-18

### Added

- Initial release of fastlimit
- Core rate limiting with Redis backend
- Token bucket and sliding window algorithms
- FastAPI integration with `@limiter.limit()` decorator
- Async-first design with full async/await support
- Rate limit headers middleware (`RateLimitHeadersMiddleware`)
- Configurable rate limit patterns (e.g., "100/minute", "1000/hour")
- Custom key extraction for rate limiting
- Comprehensive exception handling (`RateLimitExceeded`, `BackendError`)
- Optional Prometheus metrics integration
- Docker Compose setup for development
- Full test suite with pytest
