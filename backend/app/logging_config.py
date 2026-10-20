"""Logging setup for the ClipForge API.

The application owns a single ``clipforge`` logger with its own handler so that
log output is identical under uvicorn, under pytest and when the module is run
directly. Only request metadata is logged -- never bodies, headers or query
strings, which could carry credentials.
"""

from __future__ import annotations

import logging

LOGGER_NAME = "clipforge"

_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure and return the application logger."""
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_FORMAT))
        logger.addHandler(handler)
        # Keep ClipForge logs out of the root logger to avoid duplicate lines
        # when uvicorn installs its own handlers.
        logger.propagate = False
    logger.setLevel(level.upper())
    return logger
