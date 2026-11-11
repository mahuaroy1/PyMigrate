"""Ambiguous bindings are untouched; unsupported path calls are reported."""

import logging
import os


def warn_with_local_logger(logging: logging.Logger) -> None:
    logging.warn("This parameter is not the imported module.")


def keyword_path(path: str) -> bool:
    return os.path.exists(path=path)


def unpacked_path(path: str) -> bool:
    return os.path.exists(*(path,))
