"""Stage 3 - capture one real frame per flagged timestamp (graceful degradation, spec §6).

With FRAME_SELECTION=visual the timestamps are first re-chosen by looking at the video
(see `frame_selection`): the whole video is sampled at low resolution and a vision model
picks, for each state of the board/slides, the moment where it is most complete and least
blocked by the presenter. The plan is saved once, so a redelivered message skips straight
to capturing. If the visual step cannot finish within the attempt budget, the transcript-
based frames are used instead and the job carries a warning.

A frame that cannot be captured is recorded as failed and simply left out of the page;
nothing is ever substituted for it (Constraint B). Only a throttled or failing stream
lookup retries the whole stage, and on the final attempt even that degrades to a
text-only page instead of failing the job.
"""
import asyncio
import logging

from notetaker.domain.errors import ErrorCode, FatalError, RetryableError, WarningCode
from notetaker.domain.models import JobWarning, Notes, VideoMeta, format_timestamp
from notetaker.domain.status import STATUS_PROGRESS, JobStatus
from notetaker.pipeline.context import StageContext, StageResult
from notetaker.pipeline.frame_selection import scan_interval, select_frames
from notetaker.services.frames import FrameCaptureError, ScanFrame
from notetaker.services.youtube import StreamInfo

log = logging.getLogger(__name__)

_CAPTURE_TRIES = 2
_STREAM_TRIES = 2
# Scan samples are only needed until the frames are chosen.
_SCAN_CACHE_TTL_SEC = 3600
# One request carries a couple of hundred images; give it longer than a text-only call.
_MIN_SELECTION_TIMEOUT_SEC = 300


async def _scan(ctx: StageContext, interval: int) -> list[ScanFrame]:
    cached = await ctx.store.load_scan(ctx.job_id)
    if cached:
        return [ScanFrame(ts, img) for ts, img in sorted(cached.items())]
    settings = ctx.settings
    await ctx.report("Scanning the video for board and slide content")
    stream = await ctx.services.youtube.resolve_stream(ctx.job.video_id, max_height=settings.frame_scan_height)
    try:
        scan = await ctx.services.frames.scan(stream, interval, settings.frame_scan_height, settings.frame_scan_timeout_sec)
    except FrameCaptureError as exc:
        raise RetryableError(ErrorCode.YOUTUBE_FAILED, f"Could not scan the video: {exc}") from exc
    await ctx.store.save_scan(ctx.job_id, {f.timestamp_sec: f.image for f in scan}, _SCAN_CACHE_TTL_SEC)
    return scan


async def plan_frames(ctx: StageContext, notes: Notes) -> Notes:
    """Choose frame timestamps from the video itself (once per job); returns the notes to use."""
    store, settings, job_id = ctx.store, ctx.settings, ctx.job_id
    if settings.frame_selection != "visual" or await store.load_artifact_dict(job_id, "frame_plan"):
        return notes
    meta = await store.load_artifact(job_id, "meta", VideoMeta)
    if meta is None:
        raise RuntimeError("extract_frames stage ran before metadata was stored")

    interval = scan_interval(meta.duration_sec, settings.frame_scan_interval_sec, settings.frame_scan_max_images)
    try:
        scan = await _scan(ctx, interval)
        await ctx.report(f"Choosing the clearest moments from {len(scan)} sampled frames")
        frames = await select_frames(
            ctx.services.llm,
            scan,
            notes,
            meta,
            interval_sec=interval,
            max_frames=settings.max_frames,
            language=settings.notes_language,
            timeout_sec=max(settings.llm_timeout_sec, _MIN_SELECTION_TIMEOUT_SEC),
            on_wait=ctx.rate_limit_reporter("LLM"),
        )
    except (RetryableError, FatalError) as exc:
        if exc.retryable and not ctx.is_last_attempt:
            raise
        log.warning("job %s: visual frame selection unavailable, using transcript frames: %s", job_id, exc)
        await store.save_artifact(job_id, "frame_plan", {"source": "transcript", "reason": exc.message[:200]})
        await store.delete_scan(job_id)
        return notes

    log.info("job %s: visual selection chose %d frames from %d samples", job_id, len(frames), len(scan))
    notes = notes.model_copy(update={"frames": frames})
    await store.save_artifact(job_id, "notes", notes)
    await store.save_artifact(job_id, "frame_plan", {"source": "visual", "samples": len(scan)})
    await store.delete_scan(job_id)
    return notes


def plan_warnings(plan: dict | None) -> list[JobWarning]:
    if not plan or plan.get("source") != "transcript":
        return []
    return [
        JobWarning(
            code=WarningCode.VISUAL_SELECTION_UNAVAILABLE,
            message="Frames could not be chosen by looking at the video, so they were picked from the transcript "
            f"and may show the presenter in front of the board ({plan.get('reason', 'unknown reason')}).",
        )
    ]


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


async def _capture(ctx: StageContext, stream: StreamInfo, timestamp_sec: int) -> str | None:
    """Capture and store one frame. Returns None on success, else the failure reason."""
    last_error: Exception | None = None
    for _ in range(_CAPTURE_TRIES):
        try:
            image = await ctx.services.frames.grab(stream, timestamp_sec)
        except FrameCaptureError as exc:
            last_error = exc
            await asyncio.sleep(1)
            continue
        await ctx.store.save_frame(ctx.job_id, timestamp_sec, image)
        return None
    log.warning("frame %ss of job %s failed: %s", timestamp_sec, ctx.job_id, last_error)
    return str(last_error)[:200]


async def run(ctx: StageContext) -> StageResult:
    store, job_id = ctx.store, ctx.job_id
    notes = await store.load_artifact(job_id, "notes", Notes)
    if notes is None:
        raise RuntimeError("extract_frames stage ran before notes were stored")
    notes = await plan_frames(ctx, notes)

    outcomes = await store.frame_outcomes(job_id)
    pending = [f.timestamp_sec for f in notes.frames if f.timestamp_sec not in outcomes]
    base = STATUS_PROGRESS[JobStatus.EXTRACTING_FRAMES]
    span = STATUS_PROGRESS[JobStatus.PUBLISHING] - base - 5
    done = len(notes.frames) - len(pending)
    limiter = asyncio.Semaphore(ctx.settings.frame_concurrency)

    # A stream URL occasionally comes back unusable (e.g. every range request is refused).
    # When *every* frame fails on one URL, resolve a fresh one and try those frames again.
    for stream_try in range(_STREAM_TRIES):
        if not pending:
            break
        await ctx.report("Locating the video stream")
        try:
            stream = await ctx.services.youtube.resolve_stream(ctx.job.video_id)
        except RetryableError as exc:
            if not ctx.is_last_attempt:
                raise
            for ts in pending:
                await store.record_frame_outcome(job_id, ts, {"ok": False, "reason": "video stream unavailable"})
            log.warning("job %s: giving up on frames after final attempt: %s", job_id, exc)
            break

        async def capture(ts: int) -> tuple[int, str | None]:
            nonlocal done
            async with limiter:
                reason = await _capture(ctx, stream, ts)
                if reason is None:
                    done += 1
                    await ctx.report(
                        f"Captured {done} of {len(notes.frames)} frames",
                        base + span * done // max(len(notes.frames), 1),
                    )
                return ts, reason

        results = await asyncio.gather(*(capture(ts) for ts in pending))
        failed = {ts: reason for ts, reason in results if reason is not None}
        if len(failed) == len(pending) and stream_try + 1 < _STREAM_TRIES:
            log.warning("job %s: all %d frames failed on this stream; retrying with a fresh one", job_id, len(failed))
            continue
        for ts, reason in failed.items():
            await store.record_frame_outcome(job_id, ts, {"ok": False, "reason": reason})
        break

    outcomes = await store.frame_outcomes(job_id)
    if notes.frames and not any(o.get("ok") for o in outcomes.values()):
        log.warning("job %s: every frame failed; publishing a text-only page", job_id)
    warnings = plan_warnings(await store.load_artifact_dict(job_id, "frame_plan")) + frame_warnings(notes, outcomes)
    return StageResult(fields={"warnings": warnings})
