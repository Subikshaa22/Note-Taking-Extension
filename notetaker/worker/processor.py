"""Runs one stage message end to end and decides what happens to the job next.

Per message:
  1. take the job's lease (one worker per job at a time; frees itself if the holder dies)
  2. drop the message if it is stale (job finished, or already past this stage)
  3. count the attempt; a job that keeps crashing workers is dead-lettered by this count
  4. run the stage handler
  5. on success   -> publish the next stage (confirmed by the broker), then move the job
                     to the next status; the message is acked only after both
     retryable    -> publish to a delay queue (10s/30s/90s...) and record the wait, or
                     dead-letter once MAX_ATTEMPTS is used up
     fatal        -> mark FAILED
Because the ack is the very last step, a worker killed at any point leaves the message
unacknowledged and RabbitMQ hands it to another worker, which resumes from the stored
stage outputs.
"""
import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone

from aio_pika.abc import AbstractIncomingMessage
from redis.asyncio import Redis

from notetaker.config import Settings
from notetaker.domain.errors import (
    ErrorCode,
    FatalError,
    LeaseLostError,
    PipelineError,
    RateLimitedError,
    RetryableError,
)
from notetaker.domain.models import Job, JobError, RetryInfo
from notetaker.domain.status import STATUS_PROGRESS, JobStatus, Stage
from notetaker.infra.broker import Broker, Envelope
from notetaker.infra.crypto import TokenCipher, TokenCipherError
from notetaker.infra.job_store import JobStore
from notetaker.infra.lease import JobLease
from notetaker.pipeline.context import Services, StageContext, StageResult
from notetaker.pipeline.stages import HANDLERS, StageHandler

log = logging.getLogger(__name__)


class StageProcessor:
    def __init__(
        self,
        *,
        redis: Redis,
        store: JobStore,
        broker: Broker,
        services: Services,
        settings: Settings,
        cipher: TokenCipher,
        worker_id: str,
        handlers: dict[Stage, StageHandler] | None = None,
    ):
        self._redis = redis
        self._store = store
        self._broker = broker
        self._services = services
        self._settings = settings
        self._cipher = cipher
        self._worker_id = worker_id
        self._handlers = handlers or HANDLERS
        self.in_flight = 0

    # ---- Broker callback -------------------------------------------------------------

    async def handle(self, message: AbstractIncomingMessage) -> None:
        self.in_flight += 1
        try:
            try:
                envelope = Envelope.from_bytes(message.body)
            except (ValueError, KeyError, TypeError):
                log.error("malformed message %r; sending it to the DLQ", message.body[:200])
                await message.reject(requeue=False)
                return
            try:
                outcome = await self.process(envelope)
            except LeaseLostError:
                log.warning("job %s: lease lost mid-stage; requeueing", envelope.job_id)
                await message.nack(requeue=True)
                return
            except Exception:
                # Infrastructure failure (Redis/RabbitMQ unreachable). Back off a little so a
                # dependency outage does not turn into a hot redelivery loop.
                log.exception("job %s: processing failed; requeueing", envelope.job_id)
                await asyncio.sleep(2)
                await message.nack(requeue=True)
                return
            log.info("job %s stage %s: %s", envelope.job_id, envelope.stage, outcome)
            await message.ack()
        finally:
            self.in_flight -= 1

    # ---- Core logic (broker-agnostic, unit-tested directly) ---------------------------

    async def process(self, envelope: Envelope) -> str:
        lease = JobLease(self._redis, envelope.job_id, self._worker_id, self._settings.lease_ttl_sec)
        await lease.acquire()
        try:
            job = await self._store.get(envelope.job_id)
            if job is None:
                return "skipped: unknown or expired job"
            if job.status.is_terminal or not envelope.stage.accepts(job.status):
                return f"skipped: stale message (job is {job.status})"

            attempt = await self._claim(job, envelope.stage)
            if attempt > self._settings.max_attempts:
                error = JobError(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="The job was interrupted too many times (worker crashes).",
                    stage=envelope.stage.value,
                )
                await self._dead_letter(envelope, error, attempt - 1)
                return "dead-lettered: crash budget exhausted"

            ctx = StageContext(
                job=await self._store.get(job.job_id),
                store=self._store,
                services=self._services,
                settings=self._settings,
                lease=lease,
                attempt=attempt,
                decrypt_token=lambda: self._decrypt(envelope.notion_token),
            )
            try:
                result = await self._run_guarded(self._handlers[envelope.stage], ctx, lease)
            except FatalError as exc:
                await self._fail(envelope, exc)
                return f"failed: {exc.code}"
            except RetryableError as exc:
                return await self._retry_or_dead_letter(envelope, exc, attempt)
            except (LeaseLostError, asyncio.CancelledError):
                raise
            except Exception as exc:
                log.exception("job %s: unexpected error in stage %s", job.job_id, envelope.stage)
                return await self._retry_or_dead_letter(
                    envelope, RetryableError(ErrorCode.INTERNAL_ERROR, f"Unexpected error: {type(exc).__name__}"), attempt
                )
            await self._advance(envelope, result)
            return "completed stage"
        finally:
            await lease.release()

    async def _claim(self, job: Job, stage: Stage) -> int:
        attempt = await self._store.increment_attempt(job.job_id)
        if job.status == JobStatus.QUEUED:
            await self._store.transition(
                job.job_id, JobStatus.TRANSCRIBING, progress=STATUS_PROGRESS[JobStatus.TRANSCRIBING]
            )
        await self._store.update(job.job_id, worker_id=self._worker_id, retry=None)
        await self._store.add_event(job.job_id, stage=stage.value, attempt=attempt, worker=self._worker_id, note="claimed")
        return attempt

    def _decrypt(self, token: str) -> str:
        try:
            return self._cipher.decrypt(token)
        except TokenCipherError as exc:
            raise FatalError(ErrorCode.CONFIG_ERROR, str(exc)) from exc

    @staticmethod
    async def _run_guarded(handler: StageHandler, ctx: StageContext, lease: JobLease) -> StageResult:
        """Run the stage, but abandon it as soon as the lease is lost."""
        work = asyncio.create_task(handler(ctx))
        lost = asyncio.create_task(lease.lost.wait())
        done, _ = await asyncio.wait({work, lost}, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            lost.cancel()
            return work.result()
        work.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await work
        raise LeaseLostError(ctx.job_id)

    async def _advance(self, envelope: Envelope, result: StageResult) -> None:
        nxt = envelope.stage.next
        common = {"attempt": 0, "retry": None, "detail": None, **result.fields}
        if nxt is not None:
            # Publish first: if we crash before the status update, the redelivered message
            # republishes (the duplicate is discarded as stale), whereas the reverse order
            # could strand the job in a status with no message to drive it.
            await self._broker.publish_stage(Envelope(envelope.job_id, nxt, envelope.notion_token))
            await self._store.transition(envelope.job_id, nxt.status, progress=STATUS_PROGRESS[nxt.status], **common)
        else:
            status = result.final_status or JobStatus.COMPLETED
            await self._store.transition(envelope.job_id, status, progress=100, **common)

    async def _fail(self, envelope: Envelope, exc: PipelineError) -> None:
        error = JobError(code=exc.code, message=exc.message, stage=envelope.stage.value)
        await self._store.transition(envelope.job_id, JobStatus.FAILED, error=error, retry=None, detail=None)

    async def _dead_letter(self, envelope: Envelope, error: JobError, attempts: int) -> None:
        await self._broker.publish_dead_letter(envelope, error, attempts)
        await self._store.transition(envelope.job_id, JobStatus.DEAD_LETTERED, error=error, retry=None, detail=None)

    async def _retry_or_dead_letter(self, envelope: Envelope, exc: RetryableError, attempt: int) -> str:
        max_attempts = self._settings.max_attempts
        if attempt >= max_attempts:
            error = JobError(
                code=exc.code,
                message=f"{exc.message} (gave up after {attempt} attempts)",
                stage=envelope.stage.value,
            )
            await self._dead_letter(envelope, error, attempt)
            return f"dead-lettered: {exc.code}"

        delay = self._settings.retry_delay_for(attempt)
        if isinstance(exc, RateLimitedError) and exc.retry_after_sec:
            delay = max(delay, int(exc.retry_after_sec + 0.999))
        delay = self._broker.retry_delay_tier(delay)
        await self._broker.publish_retry(envelope, delay)

        next_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        retry = RetryInfo(
            attempt=attempt,
            max_attempts=max_attempts,
            next_attempt_at=next_at.isoformat(timespec="seconds"),
            delay_sec=delay,
            reason_code=exc.code,
            reason=exc.message,
        )
        await self._store.update(envelope.job_id, retry=retry, detail=None)
        await self._store.add_event(
            envelope.job_id, stage=envelope.stage.value, attempt=attempt, note=f"retry in {delay}s: {exc.code}"
        )
        return f"retry scheduled in {delay}s ({exc.code})"
