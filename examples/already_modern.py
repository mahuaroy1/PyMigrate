"""Modern APIs and migration-like text should remain unchanged."""

import logging
from pathlib import Path
import time


LEGACY_EXAMPLE = "logging.warn(message); time.clock(); os.path.exists(path)"


def inspect_path(path: str) -> tuple[bool, float]:
    logging.warning("Inspecting path: %s", path)
    started = time.perf_counter()
    return Path(path).exists(), started
