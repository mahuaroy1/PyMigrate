"""Opt-in migration: review elapsed-time versus CPU-time requirements."""

import time


def start_timer() -> float:
    return time.clock()
