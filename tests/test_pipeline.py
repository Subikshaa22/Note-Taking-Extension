import pytest

from notetaker.domain.errors import ErrorCode, FatalError, RetryableError
from notetaker.domain.models import FrameSuggestion, Job, Notes, Section, VideoMeta
from notetaker.domain.status import JobStatus
from notetaker.pipeline import extract_frames, publish, summarize, transcribe
from notetaker.pipeline.notion_page import PlacedFrame, build_page_blocks, job_marker
from notetaker.pipeline.synthesis import chunk_segments, normalize_sections, synthesize, validate_frames
from notetaker.services.frames import FrameCaptureError, ffmpeg_error_text
from notetaker.services.youtube import StreamInfo
from tests.fakes import (
    FakeFrames,
    FakeLLM,
    FakeNotion,
    FakeYouTube,
    make_context,
    make_services,
    make_settings,
    sample_notes,
    sample_transcript,
)


def _job() -> Job:
    return Job(job_id="job-1", video_id="dQw4w9WgXcQ", youtube_url="https://youtu.be/dQw4w9WgXcQ")


async def _noop_report(_detail):
    return None


# ---- Synthesis -----------------------------------------------------------------------


def test_validate_frames_clamps_dedupes_and_caps():
    frames = [
        FrameSuggestion(timestamp_sec=500, caption="late"),
        FrameSuggestion(timestamp_sec=-1, caption="negative"),
        FrameSuggestion(timestamp_sec=10, caption="first"),
        FrameSuggestion(timestamp_sec=15, caption="too close"),
        FrameSuggestion(timestamp_sec=40, caption="   "),
        FrameSuggestion(timestamp_sec=60, caption="second"),
        FrameSuggestion(timestamp_sec=600, caption="past the end"),
    ]
    kept = validate_frames(frames, duration_sec=600, max_frames=8, min_gap_sec=10)
    assert [f.timestamp_sec for f in kept] == [10, 60, 500]


def test_validate_frames_spreads_when_over_cap():
    frames = [FrameSuggestion(timestamp_sec=t, caption="c") for t in range(0, 1000, 20)]
    kept = validate_frames(frames, duration_sec=1000, max_frames=4, min_gap_sec=10)
    assert len(kept) == 4
    assert kept[0].timestamp_sec == 0 and kept[-1].timestamp_sec == 980


def test_chunk_segments_respects_budget_and_order():
    transcript = sample_transcript(n=50)
    chunks = chunk_segments(transcript.segments, max_chars=100)
    assert len(chunks) > 1
    assert [s for c in chunks for s in c] == transcript.segments


def test_normalize_sections_sorts_and_clamps():
    sections = [
        Section(heading="B", start_sec=900, bullets=["x"]),
        Section(heading="A", start_sec=-5, bullets=[" y "]),
        Section(heading="", start_sec=10, bullets=[]),
    ]
    out = normalize_sections(sections, duration_sec=600)
    assert [(s.heading, s.start_sec) for s in out] == [("A", 0), ("B", 599)]
    assert out[0].bullets == ["y"]


async def test_synthesize_single_call():
    llm = FakeLLM([sample_notes().model_dump()])
    meta = VideoMeta(video_id="v", title="T", duration_sec=600)
    notes = await synthesize(
        llm, sample_transcript(), meta, language="English", max_frames=8, min_gap_sec=10,
        chunk_chars=100_000, report=_noop_report,
    )
    assert len(llm.prompts) == 1
    assert "[00:30] line 1" in llm.prompts[0]
    assert len(notes.frames) == 2


async def test_synthesize_chunks_and_merges_in_order():
    part1 = {"sections": [{"heading": "P1", "start_sec": 0, "bullets": ["a"]}],
             "frames": [{"timestamp_sec": 30, "caption": "slide one"}]}
    part2 = {"sections": [{"heading": "P2", "start_sec": 330, "bullets": ["b"]}],
             "frames": [{"timestamp_sec": 400, "caption": "slide two"}]}
    overview = {"title": "Merged", "summary": "s", "key_takeaways": ["k"]}
    transcript = sample_transcript(n=20)
    chunks = chunk_segments(transcript.segments, max_chars=200)
    responses = [part1, part2] + [{"sections": [], "frames": []}] * (len(chunks) - 2) + [overview]
    llm = FakeLLM(responses)
    notes = await synthesize(
        llm, transcript, VideoMeta(video_id="v", title="T", duration_sec=600), language="English",
        max_frames=8, min_gap_sec=10, chunk_chars=200, report=_noop_report,
    )
    assert len(llm.prompts) == len(chunks) + 1
    assert "part 1 of" in llm.prompts[0]
    assert notes.title == "Merged"
    assert [s.heading for s in notes.sections] == ["P1", "P2"]
    assert [f.timestamp_sec for f in notes.frames] == [30, 400]


# ---- Notion page layout ---------------------------------------------------------------


def test_page_blocks_pair_every_image_with_a_caption():
    notes = sample_notes()
    meta = VideoMeta(video_id="dQw4w9WgXcQ", title="Demo", duration_sec=600)
    frames = [PlacedFrame(f.timestamp_sec, f.caption, f"u{i}") for i, f in enumerate(notes.frames)]
    blocks = build_page_blocks(notes, meta, frames, "job-1")

    assert job_marker("job-1") in "".join(t["text"]["content"] for t in blocks[0]["callout"]["rich_text"])
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 2
    for img in images:
        caption = "".join(t["text"]["content"] for t in img["image"]["caption"])
        assert len(caption) > 10
    # Every image is preceded by a visible timestamp line linking to that moment.
    for idx, block in enumerate(blocks):
        if block["type"] == "image":
            label = blocks[idx - 1]["paragraph"]["rich_text"]
            stamp = next(t for t in label if t["annotations"]["bold"])
            caption_stamp = block["image"]["caption"][0]["text"]["content"]
            assert stamp["text"]["content"] == caption_stamp
            assert "t=" in stamp["text"]["link"]["url"]
    assert "02:30" in str(blocks)  # the 150s frame
    # The diagram frame (150s) belongs to the "Diagram" section (starts at 120s).
    types = [b["type"] for b in blocks]
    diagram_heading = next(i for i, b in enumerate(blocks) if b["type"] == "heading_3" and "Diagram" in str(b))
    assert types[diagram_heading:].count("image") == 1


def test_long_text_is_split_for_notion_limits():
    notes = Notes(title="t", summary="x" * 4500)
    blocks = build_page_blocks(notes, VideoMeta(video_id="v", title="T", duration_sec=60), [], "j")
    summary = next(b for b in blocks if b["type"] == "paragraph")
    assert [len(t["text"]["content"]) for t in summary["paragraph"]["rich_text"]] == [2000, 2000, 500]


# ---- Stages ---------------------------------------------------------------------------


async def test_transcribe_rejects_long_videos_before_fetching_transcript(store, redis):
    settings = make_settings()
    youtube = FakeYouTube(VideoMeta(video_id="v", title="Long", duration_sec=20 * 60 + 1))
    services = make_services(redis, settings, youtube=youtube)
    await store.create(_job())
    with pytest.raises(FatalError) as exc:
        await transcribe.run(make_context(store, services, _job()))
    assert exc.value.code == ErrorCode.VIDEO_TOO_LONG
    assert services.transcripts.calls == 0


async def test_transcribe_is_idempotent(store, redis):
    services = make_services(redis, make_settings())
    await store.create(_job())
    ctx = make_context(store, services, _job())
    await transcribe.run(ctx)
    await transcribe.run(ctx)
    assert services.transcripts.calls == 1
    job = await store.get("job-1")
    assert job.video_title == "Demo video" and job.duration_sec == 600


async def test_summarize_skips_when_notes_exist(store, redis):
    llm = FakeLLM([sample_notes().model_dump()])
    services = make_services(redis, make_settings(), llm=llm)
    await store.create(_job())
    ctx = make_context(store, services, _job())
    await transcribe.run(ctx)
    await summarize.run(ctx)
    await summarize.run(ctx)
    assert len(llm.prompts) == 1


async def _prepare_notes(store, redis, **service_kwargs):
    services = make_services(redis, make_settings(), llm=FakeLLM([sample_notes().model_dump()]), **service_kwargs)
    await store.create(_job())
    ctx = make_context(store, services, _job())
    await transcribe.run(ctx)
    await summarize.run(ctx)
    return services


async def test_frame_failure_degrades_to_warning(store, redis):
    services = await _prepare_notes(store, redis, frames=FakeFrames(failing={150}))
    result = await extract_frames.run(make_context(store, services, _job()))
    warnings = result.fields["warnings"]
    assert [w.timestamp_sec for w in warnings] == [150]
    assert warnings[0].code == "FRAME_EXTRACTION_FAILED"
    outcomes = await store.frame_outcomes("job-1")
    assert outcomes[30]["ok"] and not outcomes[150]["ok"]


async def test_frame_stage_resumes_without_recapturing(store, redis):
    frames = FakeFrames()
    services = await _prepare_notes(store, redis, frames=frames)
    await store.save_frame("job-1", 30, b"already captured")
    await extract_frames.run(make_context(store, services, _job()))
    assert frames.calls == [150]


class _StaleFirstStream(FakeYouTube):
    """The first resolved stream URL is unusable; later ones work."""

    resolves = 0

    async def resolve_stream(self, video_id, max_height=None):
        self.resolves += 1
        return StreamInfo(url=f"https://stream/{self.resolves}", http_headers={}, height=720)


class _FramesFailOnStream(FakeFrames):
    def __init__(self, bad_urls):
        super().__init__()
        self.bad_urls = set(bad_urls)

    async def grab(self, stream, timestamp_sec):
        if stream.url in self.bad_urls:
            self.calls.append(timestamp_sec)
            raise FrameCaptureError("Server returned 403 Forbidden")
        return await super().grab(stream, timestamp_sec)


async def test_all_frames_failing_retries_with_fresh_stream(store, redis):
    youtube = _StaleFirstStream(VideoMeta(video_id="v", title="T", duration_sec=600))
    services = await _prepare_notes(store, redis, youtube=youtube,
                                    frames=_FramesFailOnStream({"https://stream/1"}))
    result = await extract_frames.run(make_context(store, services, _job()))
    assert youtube.resolves == 2
    assert result.fields["warnings"] == []
    outcomes = await store.frame_outcomes("job-1")
    assert outcomes[30]["ok"] and outcomes[150]["ok"]


async def test_all_frames_failing_on_every_stream_degrades(store, redis):
    youtube = _StaleFirstStream(VideoMeta(video_id="v", title="T", duration_sec=600))
    services = await _prepare_notes(store, redis, youtube=youtube,
                                    frames=_FramesFailOnStream({"https://stream/1", "https://stream/2"}))
    result = await extract_frames.run(make_context(store, services, _job()))
    assert youtube.resolves == 2
    assert [w.timestamp_sec for w in result.fields["warnings"]] == [30, 150]
    assert "403 Forbidden" in result.fields["warnings"][0].message


def test_ffmpeg_error_text_drops_stream_url():
    stderr = (b"[https @ 0x1] https://rr1---sn.googlevideo.com/videoplayback?expire=1&sig="
              + b"A" * 400 + b": Server returned 403 Forbidden (access denied)\n")
    text = ffmpeg_error_text(stderr)
    assert "googlevideo" not in text and "403 Forbidden" in text


async def test_stream_failure_retries_then_degrades_on_last_attempt(store, redis):
    youtube = FakeYouTube(VideoMeta(video_id="v", title="T", duration_sec=600),
                          stream_error=RetryableError(ErrorCode.YOUTUBE_FAILED, "boom"))
    services = await _prepare_notes(store, redis, youtube=youtube)
    with pytest.raises(RetryableError):
        await extract_frames.run(make_context(store, services, _job(), attempt=1))
    result = await extract_frames.run(make_context(store, services, _job(), attempt=3))
    assert len(result.fields["warnings"]) == 2


async def test_publish_creates_page_once(store, redis):
    notion = FakeNotion()
    services = await _prepare_notes(store, redis, notion=notion)
    await extract_frames.run(make_context(store, services, _job()))
    result = await publish.run(make_context(store, services, _job()))
    again = await publish.run(make_context(store, services, _job()))

    assert len(notion.pages) == 1
    assert result.final_status == JobStatus.COMPLETED
    assert result.fields["result"].frames_published == 2
    assert result.fields["result"].notion_page_url.startswith("https://www.notion.so/")
    assert again.fields["result"].notion_page_id == result.fields["result"].notion_page_id
    page_blocks = notion.children[result.fields["result"].notion_page_id]
    assert sum(b["type"] == "image" for b in page_blocks) == 2


async def test_publish_adopts_page_created_before_a_crash(store, redis):
    notion = FakeNotion()
    notion.fail_next_create_after_commit = True
    services = await _prepare_notes(store, redis, notion=notion)
    await extract_frames.run(make_context(store, services, _job()))
    with pytest.raises(RetryableError):
        await publish.run(make_context(store, services, _job()))
    assert len(notion.pages) == 1

    result = await publish.run(make_context(store, services, _job(), attempt=2))
    assert len(notion.pages) == 1, "redelivery must not create a duplicate page"
    assert result.fields["result"].notion_page_id in notion.pages


async def test_publish_text_only_when_all_frames_failed(store, redis):
    notion = FakeNotion()
    services = await _prepare_notes(store, redis, notion=notion, frames=FakeFrames(failing={30, 150}))
    frames_result = await extract_frames.run(make_context(store, services, _job()))
    job = _job().model_copy(update={"warnings": frames_result.fields["warnings"]})
    result = await publish.run(make_context(store, services, job))
    assert result.final_status == JobStatus.COMPLETED_WITH_WARNINGS
    assert result.fields["result"].frames_published == 0
    page_blocks = notion.children[result.fields["result"].notion_page_id]
    assert not any(b["type"] == "image" for b in page_blocks)
    assert len(result.fields["warnings"]) == 2


async def test_default_destination_is_shared_root_not_last_notes_page(store, redis):
    # The shared page is nested under a page the integration can't see, and the most recently
    # edited page is an earlier notes page inside it. New notes must go beside it, not inside it.
    notion = FakeNotion()
    notion.parent_parent = {"type": "page_id", "page_id": "private-root"}
    notion.pages["old-notes"] = {"title": "Earlier notes", "parent": {"type": "page_id", "page_id": "parent-page"}}
    notion.children["old-notes"] = []
    notion.search_results = [
        {"id": "old-notes", "parent": {"type": "page_id", "page_id": "parent-page"}},
        {"id": "parent-page", "parent": {"type": "page_id", "page_id": "private-root"}},
    ]
    services = await _prepare_notes(store, redis, notion=notion)
    await extract_frames.run(make_context(store, services, _job()))
    result = await publish.run(make_context(store, services, _job()))

    created = notion.pages[result.fields["result"].notion_page_id]
    assert created["parent"] == {"page_id": "parent-page"}


async def test_publish_unknown_parent_is_fatal(store, redis):
    services = await _prepare_notes(store, redis)
    job = _job().model_copy(update={"notion_parent_id": "missing-page"})
    with pytest.raises(FatalError) as exc:
        await publish.run(make_context(store, services, job))
    assert exc.value.code == ErrorCode.NOTION_PARENT_NOT_FOUND
