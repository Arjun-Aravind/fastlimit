"""
Pydantic models for configuration and data structures.
"""

from pydantic import BaseModel, Field, field_validator
from typing import Optional, Literal


class RateLimitConfig(BaseModel):
    """Configuration model for the rate limiter."""

    redis_url: str = Field(
        default="redis://localhost:6379",
        description="Redis connection URL (redis://[user:password@]host[:port][/db])",
    )
    key_prefix: str = Field(
        default="ratelimit",
        description="Prefix for all rate limit keys in Redis",
    )
    default_algorithm: Literal["fixed_window", "token_bucket"] = Field(
        default="fixed_window",
        description="Default rate limiting algorithm to use",
    )
    enable_metrics: bool = Field(
        default=False,
        description="Enable Prometheus metrics collection (future feature)",
    )
    connection_timeout: int = Field(
        default=5,
        description="Redis connection timeout in seconds",
    )
    socket_timeout: int = Field(
        default=5,
        description="Redis socket timeout in seconds",
    )
    max_connections: int = Field(
        default=50,
        description="Maximum number of Redis connections in the pool",
    )

    @field_validator("default_algorithm")
    @classmethod
    def validate_algorithm(cls, v: str) -> str:
        """Validate that the algorithm is supported."""
        valid_algorithms = ["fixed_window", "token_bucket"]
        if v not in valid_algorithms:
            raise ValueError(f"Algorithm must be one of {valid_algorithms}")
        return v

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, v: str) -> str:
        """Basic validation of Redis URL format."""
        if not v.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("Redis URL must start with redis://, rediss://, or unix://")
        return v

    @field_validator("connection_timeout", "socket_timeout", "max_connections")
    @classmethod
    def validate_positive_int(cls, v: int) -> int:
        """Validate that timeout and connection values are positive."""
        if v <= 0:
            raise ValueError("Value must be positive")
        return v

    class Config:
        """Pydantic config."""

        validate_assignment = True
        frozen = False  # Allow modification after creation
