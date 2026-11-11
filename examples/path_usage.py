"""Opt-in migration: this example uses a string path, not bytes or a descriptor."""

import os


def config_exists(path: str) -> bool:
    return os.path.exists(path)
