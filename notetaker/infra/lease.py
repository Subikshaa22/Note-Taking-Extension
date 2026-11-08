"""Per-job lease: at most one worker runs a stage of a given job at any time.

RabbitMQ only guarantees at-least-once delivery, so the same stage message can reach two
workers (e.g. a worker whose broker connection dropped keeps running while the message is
redelivered elsewhere). The lease is a Redis key with a short TTL that the holder renews in
the background. If the holder dies, the key expires within LEASE_TTL_SEC and the redelivered
message can proceed; if the holder loses the key while still alive, `lost` is set and the
holder must stop before performing further side effects.
"""
import asyncio
import contextlib
import logging

from redis.asyncio import Redis

from notetaker.domain.errors import LeaseLostError

log = logging.getLogger(__name__)

_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class JobLease:
    def __init__(self, redis: Redis, job_id: str, owner: str, ttl_sec: float):
        self._redis = redis
        self._key = f"lease:job:{job_id}"
        self._owner = owner
        self._ttl_ms = int(ttl_sec * 1000)
        self._renew = redis.register_script(_RENEW_LUA)
        self._release = redis.register_script(_RELEASE_LUA)
        self._keeper: asyncio.Task | None = None
        self.lost = asyncio.Event()

    async def acquire(self, poll_sec: float = 0.5) -> None:
        """Block until the lease is ours. A crashed holder frees it within one TTL."""
        while not await self._redis.set(self._key, self._owner, nx=True, px=self._ttl_ms):
            await asyncio.sleep(poll_sec)
        self._keeper = asyncio.create_task(self._keep_alive())

    async def _keep_alive(self) -> None:
        interval = self._ttl_ms / 3000
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._renew(keys=[self._key], args=[self._owner, self._ttl_ms])
            except Exception:  # transient Redis error: try again before the TTL runs out
                log.warning("lease renewal failed for %s", self._key, exc_info=True)
                continue
            if not renewed:
                log.error("lease %s lost", self._key)
                self.lost.set()
                return

    async def ensure_held(self) -> None:
        """Raise unless this worker still owns the lease (checked against Redis)."""
        if self.lost.is_set() or await self._redis.get(self._key) != self._owner:
            self.lost.set()
            raise LeaseLostError(self._key)

    async def release(self) -> None:
        if self._keeper:
            self._keeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keeper
        with contextlib.suppress(Exception):
            await self._release(keys=[self._key], args=[self._owner])

    async def __aenter__(self) -> "JobLease":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()
