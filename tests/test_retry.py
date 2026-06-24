"""Tests for failed-job retry timing."""

from akwarr.core.retry import is_transient_error, retry_delay_seconds


def test_is_transient_error_matches_connection_failures() -> None:
    assert is_transient_error("All connection attempts failed")
    assert is_transient_error("Temporary failure in name resolution")
    assert is_transient_error("ConnectError: connection refused")
    assert is_transient_error("Download aborted.")


def test_is_transient_error_rejects_permanent_failures() -> None:
    assert not is_transient_error("No download links")
    assert not is_transient_error("episode not found")
    assert not is_transient_error(None)


def test_retry_delay_uses_exponential_backoff_for_transient_errors() -> None:
    assert (
        retry_delay_seconds(
            error="All connection attempts failed",
            retry_count=0,
            default_after_seconds=300,
            transient_base_seconds=60,
            transient_max_seconds=600,
        )
        == 60
    )
    assert (
        retry_delay_seconds(
            error="All connection attempts failed",
            retry_count=1,
            default_after_seconds=300,
            transient_base_seconds=60,
            transient_max_seconds=600,
        )
        == 120
    )
    assert (
        retry_delay_seconds(
            error="All connection attempts failed",
            retry_count=4,
            default_after_seconds=300,
            transient_base_seconds=60,
            transient_max_seconds=600,
        )
        == 600
    )


def test_retry_delay_uses_default_for_permanent_errors() -> None:
    assert (
        retry_delay_seconds(
            error="No download links",
            retry_count=2,
            default_after_seconds=300,
            transient_base_seconds=60,
            transient_max_seconds=600,
        )
        == 300
    )
