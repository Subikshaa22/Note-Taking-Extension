"""Stage 1 - video metadata and transcript. Enforces Constraint A (max video length)."""
from notetaker.domain.errors import ErrorCode, FatalError
from notetaker.domain.models import Transcript, VideoMeta, format_timestamp
from notetaker.pipeline.context import StageContext, StageResult


def _enforce_length(meta: VideoMeta, limit_sec: int) -> None:
    if meta.duration_sec > limit_sec:
        raise FatalError(
            ErrorCode.VIDEO_TOO_LONG,
            f"The video is {format_timestamp(meta.duration_sec)} long; "
            f"the limit is {format_timestamp(limit_sec)}.",
        )


async def run(ctx: StageContext) -> StageResult:
    store, job_id = ctx.store, ctx.job_id
    limit = ctx.settings.max_video_duration_sec

    meta = await store.load_artifact(job_id, "meta", VideoMeta)
    if meta is None:
        await ctx.report("Reading video details")
        meta = await ctx.services.youtube.fetch_metadata(ctx.job.video_id)
        # Checked before the transcript is fetched: over-long videos are rejected without
        # doing any further work on them.
        _enforce_length(meta, limit)
        await store.save_artifact(job_id, "meta", meta)
    _enforce_length(meta, limit)
    await store.update(job_id, video_title=meta.title, duration_sec=meta.duration_sec or None)

    transcript = await store.load_artifact(job_id, "transcript", Transcript)
    if transcript is None:
        await ctx.report("Fetching transcript", 10)
        transcript = await ctx.services.transcripts.fetch(ctx.job.video_id)
        await store.save_artifact(job_id, "transcript", transcript)

    if not meta.duration_sec:
        # Metadata without a duration: the transcript's end is the best available length.
        meta = meta.model_copy(update={"duration_sec": transcript.end_sec})
        _enforce_length(meta, limit)
        await store.save_artifact(job_id, "meta", meta)
        await store.update(job_id, duration_sec=meta.duration_sec)

    return StageResult()
