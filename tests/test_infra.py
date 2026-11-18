import asyncio

import pytest

from notetaker.domain.errors import LeaseLostError
from notetaker.domain.models import Job, JobWarning, Notes, RetryInfo
from notetaker.domain.status import JobStatus, Stage
from notetaker.infra.broker import Broker, Envelope
from notetaker.infra.crypto import TokenCipher, TokenCipherError
from notetaker.infra.lease import JobLease
from notetaker.infra.rate_limiter import MAX_COOLDOWN_SEC, MAX_WAIT_SLICE_SEC, Quota, RateLimiter


def _job(job_id="j1") -> Job:
    return Job(job_id=job_id, video_id="dQw4w9WgXcQ", youtube_url="https://youtu.be/dQw4w9WgXcQ")


# ---- JobStore ------------------------------------------------------------------------


async def test_create_and_get_round_trip(store):
    await store.create(_job())
    job = await store.get("j1")
    assert job.status == JobStatus.QUEUED
    assert job.warnings == [] and job.error is None and job.notion_parent_id is None
    assert await store.get("missing") is None


async def test_transition_respects_state_machine(store):
    await store.create(_job())
    assert not await store.transition("j1", JobStatus.SUMMARIZING)
    assert await store.transition("j1", JobStatus.TRANSCRIBING, progress=5)
    job = await store.get("j1")
    assert job.status == JobStatus.TRANSCRIBING and job.progress == 5
    assert await store.transition("j1", JobStatus.FAILED)
    assert not await store.transition("j1", JobStatus.SUMMARIZING), "terminal states are final"
    assert [e["status"] for e in await store.events("j1")] == ["QUEUED", "TRANSCRIBING", "FAILED"]


async def test_update_json_fields(store):
    await store.create(_job())
    retry = RetryInfo(
        attempt=1, max_attempts=5, next_attempt_at="x", delay_sec=10, reason_code="RATE_LIMITED", reason="429"
    )
    await store.update("j1", warnings=[JobWarning(code="W", message="m", timestamp_sec=3)], retry=retry)
    job = await store.get("j1")
    assert job.warnings[0].timestamp_sec == 3
    assert job.retry.delay_sec == 10
    await store.update("j1", retry=None)
    assert (await store.get("j1")).retry is None
    with pytest.raises(ValueError):
        await store.update("j1", status=JobStatus.FAILED)


async def test_artifacts_and_frames(store):
    await store.create(_job())
    await store.save_artifact("j1", "notes", Notes(title="t"))
    assert (await store.load_artifact("j1", "notes", Notes)).title == "t"
    assert await store.load_artifact("j1", "other", Notes) is None
    await store.save_frame("j1", 42, b"\xff\xd8binary")
    assert await store.load_frame("j1", 42) == b"\xff\xd8binary"
    await store.record_frame_outcome("j1", 50, {"ok": False, "reason": "timeout"})
    assert (await store.frame_outcomes("j1"))[50]["ok"] is False


async def test_attempt_counter(store):
    await store.create(_job())
    assert await store.increment_attempt("j1") == 1
    assert await store.increment_attempt("j1") == 2


async def test_idempotency_key(store):
    assert await store.claim_idempotency_key("k", "j1", 60) is None
    assert await store.claim_idempotency_key("k", "j2", 60) == "j1"


# ---- Lease -----------------------------------------------------------------------------


async def test_lease_is_exclusive_until_released(redis):
    first = JobLease(redis, "j1", "worker-a", ttl_sec=5)
    await first.acquire()
    second = JobLease(redis, "j1", "worker-b", ttl_sec=5)
    waiter = asyncio.create_task(second.acquire(poll_sec=0.01))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    await first.release()
    await asyncio.wait_for(waiter, 1)
    await second.ensure_held()
    with pytest.raises(LeaseLostError):
        await first.ensure_held()
    await second.release()


async def test_lease_expires_when_holder_dies(redis):
    dead = JobLease(redis, "j1", "worker-a", ttl_sec=0.1)
    await dead.acquire()
    dead._keeper.cancel()  # simulate a crashed worker: no more renewals
    survivor = JobLease(redis, "j1", "worker-b", ttl_sec=5)
    await asyncio.wait_for(survivor.acquire(poll_sec=0.02), 2)
    await survivor.release()


# ---- Rate limiter ------------------------------------------------------------------------


async def test_rate_limiter_enforces_window(redis):
    limiter = RateLimiter(redis)
    quota = Quota("svc:req", limit=3, window_sec=60)
    for _ in range(3):
        assert await limiter.try_acquire("svc", [(quota, 1)]) == 0
    wait = await limiter.try_acquire("svc", [(quota, 1)])
    assert 59 <= wait <= 60


async def test_rate_limiter_weighted_quota_is_atomic(redis):
    limiter = RateLimiter(redis)
    requests = Quota("llm:req", limit=10, window_sec=60)
    tokens = Quota("llm:tok", limit=1000, window_sec=60)
    assert await limiter.try_acquire("llm", [(requests, 1), (tokens, 800)]) == 0
    assert await limiter.try_acquire("llm", [(requests, 1), (tokens, 300)]) > 0
    # The refused call must not have consumed a request slot.
    members = await redis.zrange("ratelimit:llm:req", 0, -1)
    assert len(members) == 1
    assert await limiter.try_acquire("llm", [(requests, 1), (tokens, 200)]) == 0


async def test_rate_limiter_cooldown_blocks_everyone(redis):
    limiter = RateLimiter(redis)
    await limiter.cooldown("notion", 5)
    wait = await limiter.try_acquire("notion", [(Quota("n", 100, 1), 1)])
    assert 4 <= wait <= 5


async def test_rate_limiter_caps_provider_cooldown(redis):
    # A 429 asking to retry in ~15 hours (daily quota) must not freeze every worker that long.
    limiter = RateLimiter(redis)
    await limiter.cooldown("llm:m", 56313)
    wait = await limiter.try_acquire("llm:m", [(Quota("q", 100, 60), 1)])
    assert 0 < wait <= MAX_COOLDOWN_SEC


async def test_rate_limiter_notices_cleared_cooldown(redis, monkeypatch):
    limiter = RateLimiter(redis)
    await limiter.cooldown("svc", 200)
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        await redis.delete("ratelimit:cooldown:svc")  # cooldown lifted while we wait

    monkeypatch.setattr("notetaker.infra.rate_limiter.asyncio.sleep", fake_sleep)
    await limiter.acquire("svc", [(Quota("q2", 100, 60), 1)])
    assert len(sleeps) == 1 and sleeps[0] <= MAX_WAIT_SLICE_SEC + 1


# ---- Broker / crypto (no RabbitMQ needed) -------------------------------------------------


def test_envelope_round_trip():
    env = Envelope(job_id="j1", stage=Stage.SUMMARIZE, notion_token="enc")
    assert Envelope.from_bytes(env.to_bytes()) == env


def test_retry_delay_tier_selection():
    broker = Broker("amqp://unused", [30, 10, 90], delivery_limit=20)
    assert broker.retry_delay_tier(1) == 10
    assert broker.retry_delay_tier(10) == 10
    assert broker.retry_delay_tier(31) == 90
    assert broker.retry_delay_tier(500) == 90


def test_token_cipher():
    from cryptography.fernet import Fernet

    cipher = TokenCipher(Fernet.generate_key().decode())
    assert cipher.decrypt(cipher.encrypt("secret_abc")) == "secret_abc"
    with pytest.raises(TokenCipherError):
        TokenCipher("")
    with pytest.raises(TokenCipherError):
        TokenCipher(Fernet.generate_key().decode()).decrypt(cipher.encrypt("x"))
