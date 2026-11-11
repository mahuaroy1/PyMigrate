"""Direct-import migrations preserve existing local binding names."""

from logging import warn as legacy_warning
from os.path import exists as path_exists
from time import clock as legacy_timer


def inspect_path(path: str) -> tuple[bool, float]:
    legacy_warning("Inspecting path: %s", path)
    started = legacy_timer()
    return path_exists(path), started
