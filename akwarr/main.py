"""CLI entrypoints for Radarr and Sonarr modes."""

import logging

import uvicorn

from akwarr.config import get_settings


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def run_radarr() -> None:
    _configure_logging()
    uvicorn.run(
        "akwarr.api.radarr:app",
        host="0.0.0.0",
        port=7879,
        log_level="info",
        factory=False,
    )


def run_sonarr() -> None:
    _configure_logging()
    uvicorn.run(
        "akwarr.api.sonarr:app",
        host="0.0.0.0",
        port=8990,
        log_level="info",
        factory=False,
    )


if __name__ == "__main__":
    mode = get_settings().mode.lower()
    if mode == "sonarr":
        run_sonarr()
    else:
        run_radarr()
