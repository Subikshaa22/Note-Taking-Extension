"""Stage 3 - capture one real frame per flagged timestamp (graceful degradation, spec §6).

A frame that cannot be captured is recorded as failed and simply left out of the page;
nothing is ever substituted for it (Constraint B). Only a throttled or failing stream
lookup retries the whole stage, and on the final attempt even that degrades to a
text-only page instead of failing the job.
"""
import asyncio
import logging

from notetaker.domain.errors import RetryableError, WarningCode
from notetaker.domain.models import JobWarning, Notes, format_timestamp
from notetaker.domain.status import STATUS_PROGRESS, JobStatus
from notetaker.pipeline.context import StageContext, StageResult
from notetaker.services.frames import FrameCaptureError
from notetaker.services.youtube import StreamInfo

log = logging.getLogger(__name__)

_CAPTURE_TRIES = 2


def frame_warnings(notes: Notes, outcomes: dict[int, dict]) -> list[JobWarning]:
    warnings = []
    for frame in notes.frames:
        outcome = outcomes.get(frame.timestamp_sec, {})
        if not outcome.get("ok"):
            reason = outcome.get("reason", "not captured")
            warnings.append(
                JobWarning(
                    code=WarningCode.FRAME_EXTRACTION_FAILED,
                    message=f"Frame at {format_timestamp(frame.timestamp_sec)} could not be captured ({reason}) and was left out.",
                    timestamp_sec=frame.timestamp_sec,
                )
            )
    return warnings


async def _capture(ctx: StageContext, stream: StreamInfo, timestamp_sec: int) -> None:
    last_error: Exception | None = None
    for _ in range(_CAPTURE_TRIES):
        try:
            image = await ctx.services.frames.grab(stream, timestamp_sec)
        except FrameCaptureError as exc:
            last_error = exc
            await asyncio.sleep(1)
            continue
        await ctx.store.save_frame(ctx.job_id, timestamp_sec, image)
        return
    log.warning("frame %ss of job %s failed: %s", timestamp_sec, ctx.job_id, last_error)
    await ctx.store.record_frame_outcome(ctx.job_id, timestamp_sec, {"ok": False, "reason": str(last_error)[:200]})


async def run(ctx: StageContext) -> StageResult:
    store, job_id = ctx.store, ctx.job_id
    notes = await store.load_artifact(job_id, "notes", Notes)
    if notes is None:
        raise RuntimeError("extract_frames stage ran before notes were stored")

    outcomes = await store.frame_outcomes(job_id)
    pending = [f.timestamp_sec for f in notes.frames if f.timestamp_sec not in outcomes]

    if pending:
        await ctx.report("Locating the video stream")
        try:
            stream = await ctx.services.youtube.resolve_stream(ctx.job.video_id)
        except RetryableError as exc:
            if not ctx.is_last_attempt:
                raise
            for ts in pending:
                await store.record_frame_outcome(job_id, ts, {"ok": False, "reason": "video stream unavailable"})
            log.warning("job %s: giving up on frames after final attempt: %s", job_id, exc)
            pending = []

        if pending:
            base = STATUS_PROGRESS[JobStatus.EXTRACTING_FRAMES]
            span = STATUS_PROGRESS[JobStatus.PUBLISHING] - base - 5
            done = len(notes.frames) - len(pending)
            limiter = asyncio.Semaphore(ctx.settings.frame_concurrency)

            async def capture(ts: int) -> None:
                nonlocal done
                async with limiter:
                    await _capture(ctx, stream, ts)
                    done += 1
                    await ctx.report(
                        f"Captured {done} of {len(notes.frames)} frames",
                        base + span * done // max(len(notes.frames), 1),
                    )

            await asyncio.gather(*(capture(ts) for ts in pending))

    outcomes = await store.frame_outcomes(job_id)
    if notes.frames and not any(o.get("ok") for o in outcomes.values()):
        log.warning("job %s: every frame failed; publishing a text-only page", job_id)
    return StageResult(fields={"warnings": frame_warnings(notes, outcomes)})
