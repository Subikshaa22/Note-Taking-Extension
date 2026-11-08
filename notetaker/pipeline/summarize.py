"""Stage 2 - LLM synthesis of notes and captioned frame timestamps."""
from notetaker.domain.models import Notes, Transcript, VideoMeta
from notetaker.pipeline.context import StageContext, StageResult
from notetaker.pipeline.synthesis import synthesize


async def run(ctx: StageContext) -> StageResult:
    store, job_id, settings = ctx.store, ctx.job_id, ctx.settings
    if await store.load_artifact(job_id, "notes", Notes) is not None:
        return StageResult()

    transcript = await store.load_artifact(job_id, "transcript", Transcript)
    meta = await store.load_artifact(job_id, "meta", VideoMeta)
    if transcript is None or meta is None:
        raise RuntimeError("summarize stage ran before transcript stage outputs were stored")

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
    await store.save_artifact(job_id, "notes", notes)
    return StageResult()
