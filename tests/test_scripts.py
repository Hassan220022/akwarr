import subprocess


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
