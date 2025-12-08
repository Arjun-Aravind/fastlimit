"""
Utility functions for rate limiting operations.
"""

import re
from typing import Tuple
from datetime import datetime
import hashlib


def parse_rate(rate_string: str) -> Tuple[int, int]:
    """
    Parse rate string into requests and window seconds.

    Supports formats like:
    - "100/second" - 100 requests per second
    - "1000/minute" - 1000 requests per minute
    - "10000/hour" - 10000 requests per hour
    - "100000/day" - 100000 requests per day

    Args:
        rate_string: Rate limit string in format "number/period"

    Returns:
        Tuple of (requests, window_seconds)

    Raises:
        ValueError: If rate string is invalid

    Examples:
        >>> parse_rate("100/minute")
        (100, 60)
        >>> parse_rate("1000/hour")
        (1000, 3600)
    """
    # Normalize input
    rate_string = rate_string.strip().lower()

    # Pattern to match rate format
    pattern = r"^(\d+)/(second|seconds|minute|minutes|hour|hours|day|days)$"
    match = re.match(pattern, rate_string)

    if not match:
        raise ValueError(
            f"Invalid rate string: '{rate_string}'. "
            f"Expected format: 'number/period' (e.g., '100/minute')"
        )

    requests = int(match.group(1))
    period = match.group(2)

    # Normalize period to singular form
    period = period.rstrip("s")

    # Convert period to seconds
    period_seconds = {
        "second": 1,
        "minute": 60,
        "hour": 3600,
        "day": 86400,
    }

    if period not in period_seconds:
        raise ValueError(f"Invalid period: {period}")

    return requests, period_seconds[period]


def generate_key(
    prefix: str, identifier: str, tenant_type: str, time_window: str
) -> str:
    """
    Generate Redis key for rate limiting.

    Creates a hierarchical key structure for efficient Redis operations
    and clear organization of rate limit data.

    Uses URL-safe encoding to prevent key collisions while keeping keys readable.
    This is important because simple character replacement (e.g., : -> _) can
    cause different identifiers to map to the same key.

    Args:
        prefix: Key prefix (e.g., "ratelimit")
        identifier: Unique identifier (e.g., IP address, user ID)
        tenant_type: Tenant type/tier (e.g., "free", "premium", "enterprise")
        time_window: Time window identifier (e.g., "1700000100")

    Returns:
        Formatted Redis key

    Examples:
        >>> generate_key("ratelimit", "192.168.1.1", "free", "1700000100")
        'ratelimit:192.168.1.1:free:1700000100'

        >>> generate_key("ratelimit", "user:123", "premium", "1700000100")
        'ratelimit:user%3A123:premium:1700000100'  # Colon encoded to prevent collision
    """
    # Use URL-safe encoding for identifier and tenant_type
    # This prevents collisions: "a:b" != "a_b" after encoding
    safe_id = _url_encode_key_component(identifier)
    safe_tenant = _url_encode_key_component(tenant_type)

    # Generate the key and apply hash optimization for long keys
    full_key = f"{prefix}:{safe_id}:{safe_tenant}:{time_window}"
    return hash_key(full_key, max_length=200)


def _url_encode_key_component(value: str) -> str:
    """
    URL-encode a key component to prevent Redis key issues and collisions.

    Only encodes characters that would cause issues:
    - Colon (:) - used as key delimiter
    - Space ( ) - causes parsing issues
    - Special Redis pattern chars (* ? [ ] { })

    Args:
        value: The string to encode

    Returns:
        URL-safe encoded string

    Examples:
        >>> _url_encode_key_component("user:123")
        'user%3A123'

        >>> _url_encode_key_component("normal_key")
        'normal_key'
    """
    from urllib.parse import quote

    # Encode only problematic characters, keep alphanumeric and common safe chars
    # safe='...' means these characters will NOT be encoded
    return quote(value, safe='-_.~')


def get_time_window(window_seconds: int) -> str:
    """
    Generate time window key based on window size.

    Creates consistent time window identifiers for grouping
    rate limit counters.

    Args:
        window_seconds: Size of the time window in seconds

    Returns:
        Time window identifier string

    Examples:
        >>> # For a 60-second window at 2024-11-01 14:35:42
        >>> get_time_window(60)
        '2024-11-01-14:35'  # Minute precision

        >>> # For a 3600-second (1 hour) window
        >>> get_time_window(3600)
        '2024-11-01-14'  # Hour precision
    """
    now = datetime.utcnow()

    if window_seconds <= 1:
        # Per second - include seconds
        return now.strftime("%Y-%m-%d-%H:%M:%S")
    elif window_seconds <= 60:
        # Per minute - minute precision
        return now.strftime("%Y-%m-%d-%H:%M")
    elif window_seconds <= 3600:
        # Per hour - hour precision
        return now.strftime("%Y-%m-%d-%H")
    elif window_seconds <= 86400:
        # Per day - day precision
        return now.strftime("%Y-%m-%d")
    else:
        # Longer periods - week precision
        # Use ISO week number for consistent weekly windows
        year, week, _ = now.isocalendar()
        return f"{year}-W{week:02d}"


def hash_key(key: str, max_length: int = 200) -> str:
    """
    Hash a key if it's too long for Redis.

    Redis keys can be up to 512MB, but very long keys impact performance.
    This function hashes keys that exceed a reasonable length.

    Args:
        key: The original key
        max_length: Maximum allowed key length before hashing

    Returns:
        Original key or hashed version if too long

    Examples:
        >>> short_key = "ratelimit:user123:free:2024"
        >>> hash_key(short_key) == short_key
        True

        >>> long_key = "ratelimit:" + "x" * 500
        >>> len(hash_key(long_key)) < len(long_key)
        True
    """
    if len(key) <= max_length:
        return key

    # Use SHA256 for consistent hashing
    key_hash = hashlib.sha256(key.encode()).hexdigest()

    # Preserve some prefix for debugging
    prefix_len = max_length - len(key_hash) - 1
    if prefix_len > 0:
        return f"{key[:prefix_len]}_{key_hash}"

    return key_hash


def calculate_cost(requests: int, window_seconds: int) -> float:
    """
    Calculate the "cost" or rate of requests.

    Useful for comparing different rate limits or calculating
    effective rates.

    Args:
        requests: Number of requests allowed
        window_seconds: Time window in seconds

    Returns:
        Requests per second rate

    Examples:
        >>> calculate_cost(100, 60)  # 100 per minute
        1.6666666666666667  # ~1.67 requests per second

        >>> calculate_cost(1000, 3600)  # 1000 per hour
        0.2777777777777778  # ~0.28 requests per second
    """
    if window_seconds <= 0:
        raise ValueError("Window seconds must be positive")

    return requests / window_seconds
