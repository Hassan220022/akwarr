import subprocess
from pathlib import Path


def test_aria2_bandwidth_limit_from_mbit() -> None:
    result = subprocess.run(
        [
            "bash",
            "-lc",
            "source scripts/aria2-bandwidth.sh; aria2_limit_from_mbit 100 60",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "7324K"


def test_aria2_bandwidth_limit_from_measured_bytes_per_second() -> None:
    result = subprocess.run(
        [
            "bash",
            "-lc",
            "source scripts/aria2-bandwidth.sh; "
            "aria2_limit_from_bytes_per_second 12500000 60",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "7324K"


def test_aria2_bandwidth_limit_rejects_invalid_percent() -> None:
    result = subprocess.run(
        [
            "bash",
            "-lc",
            "source scripts/aria2-bandwidth.sh; aria2_limit_from_mbit 100 101",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "ARIA2_BANDWIDTH_LIMIT_PERCENT" in result.stderr


def test_aria2_bandwidth_limit_rejects_invalid_measured_speed() -> None:
    result = subprocess.run(
        [
            "bash",
            "-lc",
            "source scripts/aria2-bandwidth.sh; "
            "aria2_limit_from_bytes_per_second 0 60",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "measured download speed" in result.stderr


def test_setup_homelab_measures_bandwidth_instead_of_defaulting_it() -> None:
    setup = Path("scripts/setup-homelab.sh").read_text()

    assert "measure_download_bytes_per_second" in setup
    assert "TOTAL_BANDWIDTH_MBIT" not in setup
    assert ":-100" not in setup
