"""All three migrations with qualified import aliases."""

import logging as log
import os.path as paths
import time as timer


def inspect_path(path: str) -> tuple[bool, float]:
    log.warn("Inspecting path: %s", path)
    started = timer.clock()
    return paths.exists(path), started
