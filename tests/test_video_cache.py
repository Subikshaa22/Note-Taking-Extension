"""Per-video cache: the same video submitted again reuses transcript, notes, frame choice and images."""
from notetaker.domain.errors import ErrorCode, RetryableError
from notetaker.domain.models import Job, Notes
from notetaker.pipeline import extract_frames, publish, summarize, transcribe
from tests.fakes import FakeFrames, FakeLLM, FakeNotion, FakeTranscripts, make_context, make_services, make_settings
from tests.fakes import sample_notes, sample_transcript

_VISUAL_PLAN = {
    "states": [{"start_sec": 100, "end_sec": 200, "content": "board"}],
    "frames": [{"timestamp_sec": 120, "caption": "The board with the full derivation."}],
}


def _job(job_id: str) -> Job:
    return Job(job_id=job_id, video_id="dQw4w9WgXcQ", youtube_url="https://youtu.be/dQw4w9WgXcQ")


async def _run_all(store, services, job: Job):
    await store.create(job)
    ctx = make_context(store, services, job)
    await transcribe.run(ctx)
    await summarize.run(ctx)
    frames_result = await extract_frames.run(ctx)
    # The worker writes the frames stage's warnings onto the job before publishing.
    job = job.model_copy(update=frames_result.fields)
    return await publish.run(make_context(store, services, job))


def _services(redis, llm, frames, transcripts, notion, **settings):
    return make_services(
        redis, make_settings(**settings), llm=llm, frames=frames, transcripts=transcripts, notion=notion
    )


async def test_second_run_of_a_video_skips_youtube_llm_and_capture(store, redis):
    frames, transcripts, notion = FakeFrames(), FakeTranscripts(sample_transcript()), FakeNotion()
    llm = FakeLLM([sample_notes().model_dump(), _VISUAL_PLAN])  # exactly one run's worth of answers
    services = _services(redis, llm, frames, transcripts, notion, frame_selection="visual")

    first = await _run_all(store, services, _job("job-1"))
    second = await _run_all(store, services, _job("job-2"))  # an LLM call here would hit an empty queue

    assert transcripts.calls == 1
    assert frames.scans == 1 and frames.calls == [120]  # the frame image was reused too
    assert len(notion.pages) == 2  # but every job still gets its own Notion page
    assert second.fields["result"].frames_published == first.fields["result"].frames_published == 1
    assert second.fields["warnings"] == []


async def test_fallback_frame_choice_is_not_cached(store, redis):
    busy = RetryableError(ErrorCode.LLM_FAILED, "LLM service error 503: high demand")
    frames = FakeFrames()
    llm = FakeLLM([sample_notes().model_dump(), busy, _VISUAL_PLAN])
    services = _services(redis, llm, frames, FakeTranscripts(sample_transcript()), FakeNotion(), frame_selection="visual")

    first = await _run_all(store, services, _job("job-1"))
    assert [w.code for w in first.fields["warnings"]] == ["VISUAL_SELECTION_UNAVAILABLE"]
    assert "Gemini is overloaded right now" in first.fields["warnings"][0].message
    assert "503" not in first.fields["warnings"][0].message

    second = await _run_all(store, services, _job("job-2"))
    # Notes were reused (no second notes call), but the frames were chosen visually this time.
    assert llm.responses == []
    assert second.fields["warnings"] == []
    notes = await store.load_artifact("job-2", "notes", Notes)
    assert [f.timestamp_sec for f in notes.frames] == [120]


async def test_changed_settings_do_not_reuse_notes(store, redis):
    llm = FakeLLM([sample_notes().model_dump(), sample_notes().model_dump()])
    transcripts = FakeTranscripts(sample_transcript())
    english = _services(redis, llm, FakeFrames(), transcripts, FakeNotion(), frame_selection="transcript")
    await _run_all(store, english, _job("job-1"))
    french = _services(
        redis, llm, FakeFrames(), transcripts, FakeNotion(), frame_selection="transcript", notes_language="French"
    )
    await _run_all(store, french, _job("job-2"))
    assert llm.responses == []  # the notes were written again, in the new language
    assert transcripts.calls == 1  # the transcript does not depend on that setting


async def test_cache_can_be_turned_off(store, redis):
    llm = FakeLLM([sample_notes().model_dump(), sample_notes().model_dump()])
    transcripts, frames = FakeTranscripts(sample_transcript()), FakeFrames()
    services = _services(
        redis, llm, frames, transcripts, FakeNotion(), frame_selection="transcript", video_cache_ttl_sec=0
    )
    await _run_all(store, services, _job("job-1"))
    await _run_all(store, services, _job("job-2"))
    assert transcripts.calls == 2 and llm.responses == []
    assert frames.calls == [30, 150, 30, 150]
