import asyncio
import logging

log = logging.getLogger(__name__)


async def retry_until_ready(name: str, connect, attempts: int = 30, delay_sec: float = 2.0):
    """Call `connect` until it succeeds, so services tolerate starting before their dependencies."""
    for attempt in range(1, attempts + 1):
        try:
            return await connect()
        except Exception as exc:
            if attempt == attempts:
                raise
            log.warning("%s not reachable yet (%s); retrying in %.0fs", name, type(exc).__name__, delay_sec)
            await asyncio.sleep(delay_sec)
