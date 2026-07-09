"""Centralized logging setup.

Every module logs through the standard library ``logging`` package under the
``hospitals`` namespace. Call :func:`configure_logging` once at process start
(the CLI does this for you) to get consistent, timestamped output.
"""

from __future__ import annotations

import logging
import os

_CONFIGURED = False

DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(level: int | str | None = None) -> None:
    """Configure root logging for the package.

    The level can be supplied directly, via the ``HOSPITALS_LOG_LEVEL``
    environment variable, or defaults to ``INFO``. Safe to call more than
    once; only the first call installs handlers.
    """

    global _CONFIGURED
    if level is None:
        level = os.environ.get("HOSPITALS_LOG_LEVEL", "INFO")
    if isinstance(level, str):
        level = logging.getLevelName(level.upper())

    if not _CONFIGURED:
        logging.basicConfig(
            level=level,
            format=DEFAULT_FORMAT,
            datefmt=DEFAULT_DATEFMT,
        )
        _CONFIGURED = True
    else:
        logging.getLogger().setLevel(level)

    # Keep third-party HTTP chatter out of our INFO stream.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a logger under the ``hospitals`` namespace."""

    if not name.startswith("hospitals"):
        name = f"hospitals.{name}"
    return logging.getLogger(name)
