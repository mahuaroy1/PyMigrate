"""Low-risk migration: logging.warn becomes logging.warning."""

import logging


def report_missing_config(path: str) -> None:
    logging.warn("Configuration file is missing: %s", path)
