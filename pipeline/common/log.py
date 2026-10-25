"""Logging setup shared by every pipeline entry point."""

from __future__ import annotations

import logging

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(level: int | str = logging.INFO) -> None:
    """Configure the root logger with a single stream handler.

    Safe to call more than once: later calls replace the handler rather than adding another.
    """
    logging.basicConfig(level=level, format=_FORMAT, datefmt="%Y-%m-%d %H:%M:%S", force=True)
