"""Stage 2 - LLM synthesis of notes and captioned frame timestamps.

With FRAME_SELECTION=visual the low-resolution video scan for the frames stage is started at
the same time, so it runs while the LLM writes the notes instead of after.

If the same video was processed before with the same settings, the cached notes are reused:
the final notes (frames already chosen) skip both the LLM and the visual frame selection;
draft notes skip only the LLM.
"""
import asyncio

from notetaker.domain.models import Notes, Transcript, VideoMeta
from notetaker.pipeline.context import StageContext, StageResult
from notetaker.pipeline.extract_frames import prefetch_scan
from notetaker.pipeline.synthesis import synthesize


async def run(ctx: StageContext) -> StageResult:
    store, job_id, settings = ctx.store, ctx.job_id, ctx.settings
    if await store.load_artifact(job_id, "notes", Notes) is not None:
        return StageResult()

    transcript = await store.load_artifact(job_id, "transcript", Transcript)
    meta = await store.load_artifact(job_id, "meta", VideoMeta)
    if transcript is None or meta is None:
        raise RuntimeError("summarize stage ran before transcript stage outputs were stored")

    cache, video_id = ctx.cache, ctx.job.video_id
    final = await cache.get(video_id, cache.final_notes_key())
    if final:
        await ctx.report("Reusing notes from an earlier run of this video")
        await store.save_artifact(job_id, "notes", Notes.model_validate(final))
        await store.save_artifact(job_id, "frame_plan", {"source": "cache"})
        return StageResult()
    draft = await cache.get(video_id, cache.draft_notes_key())
    if draft:
        await ctx.report("Reusing notes from an earlier run of this video")
        await store.save_artifact(job_id, "notes", Notes.model_validate(draft))
        return StageResult()

    scan_task = asyncio.create_task(prefetch_scan(ctx, meta))
    try:
        notes = await synthesize(
            ctx.services.llm,
            transcript,
            meta,
            language=settings.notes_language,
            max_frames=settings.max_frames,
            min_gap_sec=settings.min_frame_gap_sec,
            chunk_chars=settings.llm_chunk_chars,
            report=ctx.report,
            on_wait=ctx.rate_limit_reporter("LLM"),
        )
    except BaseException:
        scan_task.cancel()
        await asyncio.gather(scan_task, return_exceptions=True)
        raise
    await store.save_artifact(job_id, "notes", notes)
    await cache.put(video_id, cache.draft_notes_key(), notes.model_dump(mode="json"))
    # Let the scan finish so its samples are cached before the frames stage is queued.
    await scan_task
    return StageResult()
