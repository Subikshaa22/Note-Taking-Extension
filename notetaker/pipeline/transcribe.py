"""Stage 1 - video metadata and transcript. Enforces Constraint A (max video length).

It also checks Notion access up front (token valid, destination reachable) so a bad token
or an unshared page fails the job in seconds instead of after the LLM and frame work.
"""
from notetaker.domain.errors import ErrorCode, FatalError
from notetaker.domain.models import Transcript, VideoMeta, format_timestamp
from notetaker.pipeline.context import StageContext, StageResult
from notetaker.pipeline.publish import resolve_parent


def _enforce_length(meta: VideoMeta, limit_sec: int) -> None:
    if meta.duration_sec > limit_sec:
        raise FatalError(
            ErrorCode.VIDEO_TOO_LONG,
            f"The video is {format_timestamp(meta.duration_sec)} long; "
            f"the limit is {format_timestamp(limit_sec)}.",
        )


async def check_notion_access(ctx: StageContext) -> None:
    """Resolve the Notion destination now; the publish stage reuses the stored result."""
    state = await ctx.store.load_artifact_dict(ctx.job_id, "publish") or {}
    if "parent" in state:
        return
    await ctx.report("Checking access to Notion")
    notion = ctx.services.notion(ctx.decrypt_token())
    state["parent"] = await resolve_parent(
        notion, ctx.job.notion_parent_id, ctx.settings.notion_default_parent_id or None
    )
    await ctx.store.save_artifact(ctx.job_id, "publish", state)


async def run(ctx: StageContext) -> StageResult:
    store, job_id = ctx.store, ctx.job_id
    limit = ctx.settings.max_video_duration_sec

    video_id, cache = ctx.job.video_id, ctx.cache
    meta = await store.load_artifact(job_id, "meta", VideoMeta)
    if meta is None:
        await ctx.report("Reading video details")
        cached = await cache.get(video_id, "meta")
        meta = VideoMeta.model_validate(cached) if cached else await ctx.services.youtube.fetch_metadata(video_id)
        # Checked before the transcript is fetched: over-long videos are rejected without
        # doing any further work on them.
        _enforce_length(meta, limit)
        await store.save_artifact(job_id, "meta", meta)
        if not cached and meta.duration_sec:
            await cache.put(video_id, "meta", meta.model_dump(mode="json"))
    _enforce_length(meta, limit)
    await store.update(job_id, video_title=meta.title, duration_sec=meta.duration_sec or None)

    await check_notion_access(ctx)

    transcript = await store.load_artifact(job_id, "transcript", Transcript)
    if transcript is None:
        await ctx.report("Fetching transcript", 10)
        cached = await cache.get(video_id, cache.transcript_key())
        if cached:
            transcript = Transcript.model_validate(cached)
        else:
            transcript = await ctx.services.transcripts.fetch(video_id)
            await cache.put(video_id, cache.transcript_key(), transcript.model_dump(mode="json"))
        await store.save_artifact(job_id, "transcript", transcript)

    if not meta.duration_sec:
        # Metadata without a duration: the transcript's end is the best available length.
        meta = meta.model_copy(update={"duration_sec": transcript.end_sec})
        _enforce_length(meta, limit)
        await store.save_artifact(job_id, "meta", meta)
        await store.update(job_id, duration_sec=meta.duration_sec)

    return StageResult()
