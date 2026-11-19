"""Stage 3 - capture one real frame per flagged timestamp (graceful degradation, spec §6).

With FRAME_SELECTION=visual the timestamps are first re-chosen by looking at the video
(see `frame_selection`): the whole video is sampled at low resolution and a vision model
picks, for each state of the board/slides, the moment where it is most complete and least
blocked by the presenter. The plan is saved once, so a redelivered message skips straight
to capturing. The scan itself is usually started early, while the summarize stage is
writing the notes (`prefetch_scan`). If the vision call fails for a reason other than a
rate limit (timeout, overload) more than VISUAL_SELECTION_ATTEMPTS times, or the attempt
budget runs out, the transcript-based frames are used instead and the job carries a warning.

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
from notetaker.services.llm import describe_llm_failure
from notetaker.services.youtube import StreamInfo

log = logging.getLogger(__name__)

_CAPTURE_TRIES = 2
_STREAM_TRIES = 2
# Scan samples are only needed until the frames are chosen.
_SCAN_CACHE_TTL_SEC = 3600


async def _scan(ctx: StageContext, interval: int, *, quiet: bool = False) -> list[ScanFrame]:
    cached = await ctx.store.load_scan(ctx.job_id)
    if cached:
        return [ScanFrame(ts, img) for ts, img in sorted(cached.items())]
    settings = ctx.settings
    if not quiet:
        await ctx.report("Scanning the video for board and slide content")
    # A stream URL sometimes comes back unusable (e.g. HTTP 403 on every request); a freshly
    # resolved one usually works, so try that before spending a whole stage attempt.
    for stream_try in range(_STREAM_TRIES):
        stream = await ctx.services.youtube.resolve_stream(ctx.job.video_id, max_height=settings.frame_scan_height)
        try:
            scan = await ctx.services.frames.scan(
                stream, interval, settings.frame_scan_height, settings.frame_scan_timeout_sec
            )
            break
        except FrameCaptureError as exc:
            if stream_try + 1 >= _STREAM_TRIES:
                raise RetryableError(ErrorCode.YOUTUBE_FAILED, f"Could not scan the video: {exc}") from exc
            log.warning("job %s: video scan failed (%s); retrying with a fresh stream", ctx.job_id, str(exc)[:200])
    await ctx.store.save_scan(ctx.job_id, {f.timestamp_sec: f.image for f in scan}, _SCAN_CACHE_TTL_SEC)
    return scan


def _scan_interval_for(ctx: StageContext, meta: VideoMeta) -> int:
    s = ctx.settings
    return scan_interval(meta.duration_sec, s.frame_scan_interval_sec, s.frame_scan_max_images)


async def prefetch_scan(ctx: StageContext, meta: VideoMeta) -> None:
    """Sample the video ahead of time (run alongside the summarize stage's LLM call).

    The samples go into the same cache `plan_frames` reads, so the frames stage starts straight
    at the vision call. Best effort: any failure is ignored and the frames stage scans again.
    """
    if ctx.settings.frame_selection != "visual" or await ctx.store.load_artifact_dict(ctx.job_id, "frame_plan"):
        return
    try:
        await _scan(ctx, _scan_interval_for(ctx, meta), quiet=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - the frames stage retries the scan on its own
        log.info("job %s: early video scan failed, the frames stage will retry it: %s", ctx.job_id, exc)


def _should_retry_selection(ctx: StageContext, exc: RetryableError | FatalError) -> bool:
    """Retry the stage (instead of falling back to transcript frames) only for throttling, scan
    problems, or while the vision call still has attempts left."""
    if not exc.retryable or ctx.is_last_attempt:
        return False
    if exc.code in (ErrorCode.LLM_FAILED, ErrorCode.LLM_OVERLOADED):  # timeout or overload, not a rate limit
        return ctx.attempt < ctx.settings.visual_selection_attempts
    return True


async def _needs_visual_plan(ctx: StageContext) -> bool:
    return ctx.settings.frame_selection == "visual" and not await ctx.store.load_artifact_dict(ctx.job_id, "frame_plan")


async def plan_frames(ctx: StageContext, notes: Notes) -> Notes:
    """Choose frame timestamps from the video itself (once per job); returns the notes to use."""
    store, settings, job_id = ctx.store, ctx.settings, ctx.job_id
    if not await _needs_visual_plan(ctx):
        return notes
    meta = await store.load_artifact(job_id, "meta", VideoMeta)
    if meta is None:
        raise RuntimeError("extract_frames stage ran before metadata was stored")

    interval = _scan_interval_for(ctx, meta)
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
            timeout_sec=settings.frame_selection_timeout_sec,
            on_wait=ctx.rate_limit_reporter("LLM"),
            on_status=ctx.report,
        )
    except (RetryableError, FatalError) as exc:
        if _should_retry_selection(ctx, exc):
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


def _fallback_reason(reason: str) -> str:
    if reason.startswith(("LLM", "Gemini")) or "model" in reason.lower():
        return describe_llm_failure(reason)
    return "the video could not be scanned"


def plan_warnings(plan: dict | None) -> list[JobWarning]:
    if not plan or plan.get("source") != "transcript":
        return []
    return [
        JobWarning(
            code=WarningCode.VISUAL_SELECTION_UNAVAILABLE,
            message="Frames were picked from the transcript instead of by looking at the video, because "
            f"{_fallback_reason(plan.get('reason', ''))}. Some may show the presenter in front of the board.",
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
        await ctx.cache.put_frame(ctx.job.video_id, timestamp_sec, image)
        return None
    log.warning("frame %ss of job %s failed: %s", timestamp_sec, ctx.job_id, last_error)
    return str(last_error)[:200]


async def _cache_final_notes(ctx: StageContext, notes: Notes) -> None:
    """Remember the final frame choice for this video, unless it is a degraded fallback."""
    plan = await ctx.store.load_artifact_dict(ctx.job_id, "frame_plan") or {}
    good = plan.get("source") == "visual" or (ctx.settings.frame_selection == "transcript" and not plan)
    if good:
        await ctx.cache.put(ctx.job.video_id, ctx.cache.final_notes_key(), notes.model_dump(mode="json"))


async def _reuse_cached_frames(ctx: StageContext, notes: Notes) -> None:
    """Frames captured for this video by an earlier job are reused instead of captured again."""
    outcomes = await ctx.store.frame_outcomes(ctx.job_id)
    reused = 0
    for frame in notes.frames:
        if frame.timestamp_sec in outcomes:
            continue
        image = await ctx.cache.get_frame(ctx.job.video_id, frame.timestamp_sec)
        if image:
            await ctx.store.save_frame(ctx.job_id, frame.timestamp_sec, image)
            reused += 1
    if reused:
        await ctx.report(f"Reusing {reused} saved frames from an earlier run")


async def run(ctx: StageContext) -> StageResult:
    store, job_id = ctx.store, ctx.job_id
    notes = await store.load_artifact(job_id, "notes", Notes)
    if notes is None:
        raise RuntimeError("extract_frames stage ran before notes were stored")
    notes = await plan_frames(ctx, notes)
    await _cache_final_notes(ctx, notes)
    await _reuse_cached_frames(ctx, notes)

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
