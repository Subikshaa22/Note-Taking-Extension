import asyncio
import json

import pytest
from cryptography.fernet import Fernet

from notetaker.domain.errors import ErrorCode, FatalError, RateLimitedError, RetryableError
from notetaker.domain.models import Job, JobResult
from notetaker.domain.status import JobStatus, Stage
from notetaker.infra.broker import Broker, Envelope
from notetaker.infra.crypto import TokenCipher
from notetaker.pipeline.context import StageResult
from notetaker.worker.processor import StageProcessor
from tests.fakes import make_services, make_settings

KEY = Fernet.generate_key().decode()


class FakeBroker:
    def __init__(self, retry_delays):
        self.stage_messages: list[Envelope] = []
        self.retries: list[tuple[Envelope, int]] = []
        self.dead: list[tuple[Envelope, str, int]] = []
        self._tiers = Broker("amqp://unused", retry_delays, 20)

    def retry_delay_tier(self, wanted):
        return self._tiers.retry_delay_tier(wanted)

    async def publish_stage(self, envelope):
        self.stage_messages.append(envelope)

    async def publish_retry(self, envelope, delay):
        self.retries.append((envelope, delay))

    async def publish_dead_letter(self, envelope, error, attempts):
        self.dead.append((envelope, error.code, attempts))


def ok(**fields):
    async def handler(ctx):
        return StageResult(fields=fields)

    return handler


def raising(exc):
    async def handler(ctx):
        raise exc

    return handler


def all_ok():
    return {
        Stage.TRANSCRIBE: ok(),
        Stage.SUMMARIZE: ok(),
        Stage.EXTRACT_FRAMES: ok(),
        Stage.PUBLISH: ok_final(),
    }


def ok_final():
    async def handler(ctx):
        result = JobResult(notion_page_url="https://www.notion.so/p", notion_page_id="p", frames_requested=1, frames_published=1)
        return StageResult(fields={"result": result}, final_status=JobStatus.COMPLETED)

    return handler


@pytest.fixture
def settings():
    return make_settings(max_attempts=3, retry_delays_sec=[10, 30, 90], lease_ttl_sec=5)


@pytest.fixture
async def make_processor(redis, store, settings):
    def factory(handlers):
        broker = FakeBroker(settings.retry_delays_sec)
        processor = StageProcessor(
            redis=redis,
            store=store,
            broker=broker,
            services=make_services(redis, settings),
            settings=settings,
            cipher=TokenCipher(KEY),
            worker_id="worker-test",
            handlers=handlers,
        )
        return processor, broker

    return factory


async def _new_job(store) -> Envelope:
    await store.create(Job(job_id="j1", video_id="dQw4w9WgXcQ", youtube_url="https://youtu.be/dQw4w9WgXcQ"))
    return Envelope("j1", Stage.TRANSCRIBE, TokenCipher(KEY).encrypt("secret"))


async def test_happy_path_walks_every_status(store, make_processor):
    processor, broker = make_processor(all_ok())
    envelope = await _new_job(store)
    seen = []
    while True:
        await processor.process(envelope)
        seen.append((await store.get("j1")).status)
        if not broker.stage_messages:
            break
        envelope = broker.stage_messages.pop(0)
    assert seen == [
        JobStatus.SUMMARIZING,
        JobStatus.EXTRACTING_FRAMES,
        JobStatus.PUBLISHING,
        JobStatus.COMPLETED,
    ]
    job = await store.get("j1")
    assert job.progress == 100 and job.result.notion_page_id == "p" and job.attempt == 0


async def test_stale_duplicate_is_skipped(store, make_processor):
    processor, broker = make_processor(all_ok())
    envelope = await _new_job(store)
    await processor.process(envelope)
    outcome = await processor.process(envelope)  # redelivered duplicate of TRANSCRIBE
    assert outcome.startswith("skipped")
    assert len(broker.stage_messages) == 1


async def test_retryable_error_schedules_backoff(store, make_processor):
    processor, broker = make_processor({**all_ok(), Stage.TRANSCRIBE: raising(RetryableError(ErrorCode.YOUTUBE_FAILED, "x"))})
    envelope = await _new_job(store)
    await processor.process(envelope)
    job = await store.get("j1")
    assert job.status == JobStatus.TRANSCRIBING
    assert job.retry.attempt == 1 and job.retry.delay_sec == 10
    assert broker.retries[0][1] == 10

    await processor.process(envelope)
    assert broker.retries[1][1] == 30


async def test_rate_limit_respects_retry_after(store, make_processor):
    processor, broker = make_processor({**all_ok(), Stage.TRANSCRIBE: raising(RateLimitedError("llm", "429", 45))})
    await processor.process(await _new_job(store))
    assert broker.retries[0][1] == 90
    assert (await store.get("j1")).retry.reason_code == ErrorCode.RATE_LIMITED


async def test_exhausted_retries_go_to_dlq(store, make_processor, settings):
    processor, broker = make_processor({**all_ok(), Stage.TRANSCRIBE: raising(RetryableError(ErrorCode.VIDEO_UNAVAILABLE, "private"))})
    envelope = await _new_job(store)
    for _ in range(settings.max_attempts):
        await processor.process(envelope)
    job = await store.get("j1")
    assert job.status == JobStatus.DEAD_LETTERED
    assert job.error.code == ErrorCode.VIDEO_UNAVAILABLE
    assert len(broker.retries) == settings.max_attempts - 1
    assert broker.dead == [(envelope, ErrorCode.VIDEO_UNAVAILABLE, settings.max_attempts)]


async def test_fatal_error_fails_without_retry(store, make_processor):
    processor, broker = make_processor({**all_ok(), Stage.TRANSCRIBE: raising(FatalError(ErrorCode.VIDEO_TOO_LONG, "too long"))})
    await processor.process(await _new_job(store))
    job = await store.get("j1")
    assert job.status == JobStatus.FAILED
    assert job.error.code == ErrorCode.VIDEO_TOO_LONG and job.error.stage == "transcribe"
    assert not broker.retries and not broker.dead


async def test_unexpected_exception_is_retried_not_lost(store, make_processor):
    processor, broker = make_processor({**all_ok(), Stage.TRANSCRIBE: raising(KeyError("bug"))})
    await processor.process(await _new_job(store))
    assert (await store.get("j1")).retry.reason_code == ErrorCode.INTERNAL_ERROR
    assert len(broker.retries) == 1


async def test_crash_loop_is_dead_lettered(store, make_processor, settings):
    """Workers that die mid-stage never report back; the claim counter still bounds it."""
    processor, broker = make_processor(all_ok())
    envelope = await _new_job(store)
    for _ in range(settings.max_attempts):
        await store.increment_attempt("j1")  # simulated crashed attempts
    outcome = await processor.process(envelope)
    assert outcome.startswith("dead-lettered")
    assert (await store.get("j1")).status == JobStatus.DEAD_LETTERED


async def test_redelivery_after_crash_waits_for_lease_then_resumes(redis, store, make_processor, settings):
    processor, broker = make_processor(all_ok())
    envelope = await _new_job(store)
    # A crashed worker still "holds" the job; its lease expires shortly.
    await redis.set("lease:job:j1", "dead-worker", px=300)
    outcome = await asyncio.wait_for(processor.process(envelope), 3)
    assert outcome == "completed stage"
    assert (await store.get("j1")).status == JobStatus.SUMMARIZING


async def test_handle_rejects_malformed_message(make_processor):
    processor, _ = make_processor(all_ok())

    class Msg:
        body = b"not json"
        rejected = None

        async def reject(self, requeue=False):
            Msg.rejected = requeue

    await processor.handle(Msg())
    assert Msg.rejected is False


async def test_handle_acks_after_processing(store, make_processor):
    processor, _ = make_processor(all_ok())
    envelope = await _new_job(store)

    class Msg:
        body = envelope.to_bytes()
        acked = False

        async def ack(self):
            Msg.acked = True

    await processor.handle(Msg())
    assert Msg.acked
    events = await store.events("j1")
    assert any(e.get("worker") == "worker-test" for e in events)
    assert json.dumps(events)
