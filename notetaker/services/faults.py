"""Opt-in fault injection, used to demonstrate failure handling on demand.

Every rate defaults to 0 (disabled). Setting e.g. FAULT_LLM_429_RATE=0.5 makes half of the
LLM calls fail with a synthetic 429 that travels the exact same path as a real one.
"""
import logging
import random

log = logging.getLogger(__name__)


def should_inject(rate: float, what: str) -> bool:
    if rate > 0 and random.random() < rate:
        log.warning("fault injection: simulating %s", what)
        return True
    return False
