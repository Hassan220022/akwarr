"""Failed-job retry timing helpers."""

from __future__ import annotations

_TRANSIENT_ERROR_MARKERS = (
    "all connection attempts failed",
    "connection refused",
    "connection reset",
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname provided",
    "network is unreachable",
    "timed out",
    "timeout",
    "dns",
    "connect error",
    "server disconnected",
    "remote protocol error",
)


def is_transient_error(error: str | None) -> bool:
    if not error:
        return False
    lower = error.lower()
    return any(marker in lower for marker in _TRANSIENT_ERROR_MARKERS)


def retry_delay_seconds(
    *,
    error: str | None,
    retry_count: int,
    default_after_seconds: int,
    transient_base_seconds: int,
    transient_max_seconds: int,
) -> int:
    """Seconds to wait after failure before the job is eligible for retry."""
    if is_transient_error(error):
        delay = transient_base_seconds * (2**max(retry_count, 0))
        return min(delay, transient_max_seconds)
    return default_after_seconds
