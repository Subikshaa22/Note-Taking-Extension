"""Global (cross-worker) rate limiting for external APIs, coordinated through Redis.

Each quota is a sliding window ("at most N units per W seconds") stored as a sorted set of
grants. A single Lua script checks every quota of a call atomically and only records the
grant when all of them have room, so concurrent workers can never jointly exceed a limit.
When a call is refused the script returns how long to wait until enough of the window has
expired; callers sleep and try again.

A per-service cooldown key lets a worker that still receives a 429 (e.g. quota shared with
another client) pause every worker for the server's Retry-After period.
"""
import asyncio
import logging
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from redis.asyncio import Redis

log = logging.getLogger(__name__)

_ACQUIRE_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local cooldown = redis.call('PTTL', KEYS[1])
if cooldown > 0 then return cooldown end

local function cost_of(member) return tonumber(string.match(member, ':(%d+)$')) end

local wait = 0
for i = 2, #KEYS do
  local base = (i - 2) * 3
  local limit = tonumber(ARGV[base + 2])
  local window = tonumber(ARGV[base + 3])
  local cost = tonumber(ARGV[base + 4])
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now - window)
  local entries = redis.call('ZRANGE', KEYS[i], 0, -1, 'WITHSCORES')
  local used = 0
  for j = 1, #entries, 2 do used = used + cost_of(entries[j]) end
  if used + cost > limit then
    local need = used + cost - limit
    local freed = 0
    local until_free = window
    for j = 1, #entries, 2 do
      freed = freed + cost_of(entries[j])
      if freed >= need then
        until_free = tonumber(entries[j + 1]) + window - now
        break
      end
    end
    if until_free > wait then wait = until_free end
  end
end
if wait > 0 then return math.max(wait, 1) end

for i = 2, #KEYS do
  local base = (i - 2) * 3
  local window = tonumber(ARGV[base + 3])
  local cost = tonumber(ARGV[base + 4])
  redis.call('ZADD', KEYS[i], now, ARGV[1] .. ':' .. cost)
  redis.call('PEXPIRE', KEYS[i], window)
end
return 0
"""


@dataclass(frozen=True)
class Quota:
    name: str
    limit: int
    window_sec: float


WaitCallback = Callable[[float], Awaitable[None]]


class RateLimiter:
    def __init__(self, redis: Redis, prefix: str = "ratelimit"):
        self._redis = redis
        self._prefix = prefix
        self._script = redis.register_script(_ACQUIRE_LUA)

    def _cooldown_key(self, service: str) -> str:
        return f"{self._prefix}:cooldown:{service}"

    async def try_acquire(self, service: str, charges: list[tuple[Quota, int]]) -> float:
        """Reserve capacity on every quota; returns 0 on success, else seconds to wait."""
        keys = [self._cooldown_key(service)]
        args: list = [uuid.uuid4().hex]
        for quota, cost in charges:
            keys.append(f"{self._prefix}:{quota.name}")
            args.extend((quota.limit, int(quota.window_sec * 1000), max(0, min(cost, quota.limit))))
        wait_ms = int(await self._script(keys=keys, args=args))
        return wait_ms / 1000

    async def acquire(
        self,
        service: str,
        charges: list[tuple[Quota, int]],
        on_wait: WaitCallback | None = None,
    ) -> None:
        """Block until every quota grants the call."""
        while (wait := await self.try_acquire(service, charges)) > 0:
            if on_wait:
                await on_wait(wait)
            # Jitter spreads out workers that were all told to wait the same amount.
            await asyncio.sleep(wait + random.uniform(0.05, 0.25))

    async def cooldown(self, service: str, seconds: float) -> None:
        """Pause every worker's calls to `service` (used after an unexpected 429)."""
        ms = max(int(seconds * 1000), 1)
        key = self._cooldown_key(service)
        current = await self._redis.pttl(key)
        if current is None or current < ms:
            await self._redis.set(key, "1", px=ms)
        log.warning("rate-limit cooldown for %s: %.1fs", service, seconds)


class ServiceLimits:
    """Named quota sets for the three external services the pipeline calls."""

    def __init__(self, limiter: RateLimiter, *, llm_rpm: int, llm_tpm: int, notion_rps: int, youtube_rpm: int):
        self.limiter = limiter
        self._llm_rpm = llm_rpm
        self._llm_tpm = llm_tpm
        self._notion = Quota("notion:requests", notion_rps, 1)
        self._youtube = Quota("youtube:requests", youtube_rpm, 60)

    async def llm(self, model: str, estimated_tokens: int, on_wait: WaitCallback | None = None) -> None:
        # Provider quotas are per model, so each model gets its own windows.
        await self.limiter.acquire(
            f"llm:{model}",
            [
                (Quota(f"llm:{model}:requests", self._llm_rpm, 60), 1),
                (Quota(f"llm:{model}:tokens", self._llm_tpm, 60), estimated_tokens),
            ],
            on_wait,
        )

    async def notion(self, on_wait: WaitCallback | None = None) -> None:
        await self.limiter.acquire("notion", [(self._notion, 1)], on_wait)

    async def youtube(self, on_wait: WaitCallback | None = None) -> None:
        await self.limiter.acquire("youtube", [(self._youtube, 1)], on_wait)

    async def cooldown(self, service: str, seconds: float) -> None:
        await self.limiter.cooldown(service, seconds)
