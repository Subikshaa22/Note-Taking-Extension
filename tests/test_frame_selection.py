"""Visual frame selection: scanning the video and letting the vision model pick timestamps."""
import io

from PIL import Image

from notetaker.domain.errors import ErrorCode, FatalError, OverloadedError, RateLimitedError, RetryableError
from notetaker.domain.models import FrameSuggestion, Job, Notes, VideoMeta
from notetaker.pipeline import extract_frames, summarize, transcribe
from notetaker.pipeline.frame_selection import (
    build_selection_prompt,
    label_frame,
    scan_interval,
    validate_visual_frames,
)
from notetaker.services.frames import ScanFrame, parse_showinfo_times
from tests.fakes import FakeFrames, FakeLLM, make_context, make_services, make_settings, sample_jpeg, sample_notes

_VISUAL_PLAN = {
    "states": [
        {"start_sec": 100, "end_sec": 200, "content": "array and best case"},
        {"start_sec": 300, "end_sec": 450, "content": "average case derivation"},
    ],
    "frames": [
        {"timestamp_sec": 302, "caption": "Average case: (1+2+...+n)/n = (n+1)/2."},
        {"timestamp_sec": 120, "caption": "Array A with indices 0-9 and B(n) = O(1)."},
    ],
}


def _job() -> Job:
    return Job(job_id="job-1", video_id="dQw4w9WgXcQ", youtube_url="https://youtu.be/dQw4w9WgXcQ")


async def _prepare(store, redis, visual_responses, frames=None, **settings):
    llm = FakeLLM([sample_notes().model_dump(), *visual_responses])
    services = make_services(
        redis, make_settings(frame_selection="visual", **settings), llm=llm, frames=frames or FakeFrames()
    )
    await store.create(_job())
    ctx = make_context(store, services, _job())
    await transcribe.run(ctx)
    await summarize.run(ctx)
    return services


async def test_visual_selection_replaces_transcript_frames(store, redis):
    services = await _prepare(store, redis, [_VISUAL_PLAN])
    result = await extract_frames.run(make_context(store, services, _job()))

    notes = await store.load_artifact("job-1", "notes", Notes)
    assert [f.timestamp_sec for f in notes.frames] == [120, 300]  # sorted, 302 snapped to a scanned second
    assert notes.frames[1].caption.startswith("Average case")
    assert services.frames.calls == [120, 300]  # captured at the chosen seconds, not the transcript's 30/150
    assert result.fields["warnings"] == []
    # The model was shown labelled scan samples, each preceded by its timestamp.
    parts = services.llm.prompts[-1]
    assert any(isinstance(p, bytes) for p in parts) and "t=0s (00:00):" in parts


async def test_visual_plan_is_not_repeated_on_redelivery(store, redis):
    frames = FakeFrames()
    services = await _prepare(store, redis, [_VISUAL_PLAN], frames=frames)
    await extract_frames.run(make_context(store, services, _job()))
    await extract_frames.run(make_context(store, services, _job()))  # would pop an empty FakeLLM queue
    assert frames.scans == 1
    assert await store.load_scan("job-1") == {}  # samples are dropped once frames are chosen


async def test_selection_retry_reuses_the_scan(store, redis):
    frames = FakeFrames()
    busy = RetryableError(ErrorCode.LLM_FAILED, "503 high demand")
    services = await _prepare(store, redis, [busy, _VISUAL_PLAN], frames=frames, visual_selection_attempts=2)
    try:
        await extract_frames.run(make_context(store, services, _job(), attempt=1))
    except RetryableError:
        pass
    else:
        raise AssertionError("a transient selection failure should retry the stage")
    await extract_frames.run(make_context(store, services, _job(), attempt=2))
    assert frames.scans == 1
    assert frames.calls == [120, 300]


async def test_selection_falls_back_to_transcript_frames_on_last_attempt(store, redis):
    busy = RetryableError(ErrorCode.LLM_FAILED, "503 high demand")
    services = await _prepare(store, redis, [busy])
    result = await extract_frames.run(make_context(store, services, _job(), attempt=3))
    assert services.frames.calls == [30, 150]
    assert [w.code for w in result.fields["warnings"]] == ["VISUAL_SELECTION_UNAVAILABLE"]


async def test_overloaded_selection_falls_back_on_first_failure_by_default(store, redis):
    busy = OverloadedError(ErrorCode.LLM_OVERLOADED, "The AI service is overloaded (503): high demand")
    services = await _prepare(store, redis, [busy])
    result = await extract_frames.run(make_context(store, services, _job(), attempt=1))
    assert services.frames.calls == [30, 150]  # transcript frames, no retry wait
    assert [w.code for w in result.fields["warnings"]] == ["VISUAL_SELECTION_UNAVAILABLE"]


async def test_rate_limited_selection_is_still_requeued(store, redis):
    throttled = RateLimitedError("llm:gemini", "429", 10)
    services = await _prepare(store, redis, [throttled])
    try:
        await extract_frames.run(make_context(store, services, _job(), attempt=1))
    except RateLimitedError:
        pass
    else:
        raise AssertionError("a 429 must be re-queued with backoff, not degraded")


async def test_selection_uses_its_own_timeout(store, redis):
    services = await _prepare(store, redis, [_VISUAL_PLAN], frame_selection_timeout_sec=42)
    seen = {}
    original = services.llm.generate_from_parts

    async def spy(parts, schema, on_wait=None, timeout_sec=None, image_resolution="high", **kwargs):
        seen["timeout"] = timeout_sec
        return await original(parts, schema, on_wait, timeout_sec, image_resolution, **kwargs)

    services.llm.generate_from_parts = spy
    await extract_frames.run(make_context(store, services, _job()))
    assert seen["timeout"] == 42


async def test_summarize_scans_the_video_ahead_of_the_frames_stage(store, redis):
    frames = FakeFrames()
    await _prepare(store, redis, [_VISUAL_PLAN], frames=frames)
    assert frames.scans == 1  # scanned while the notes were written
    assert await store.load_scan("job-1")  # samples cached for the frames stage


async def test_failed_early_scan_does_not_fail_summarize(store, redis):
    from notetaker.services.frames import FrameCaptureError

    frames = FakeFrames(scan_error=FrameCaptureError("403"))
    await _prepare(store, redis, [_VISUAL_PLAN], frames=frames)  # would raise if summarize failed
    assert await store.load_artifact("job-1", "notes", Notes) is not None
    assert await store.load_scan("job-1") == {}


async def test_transcript_mode_does_not_scan(store, redis):
    frames = FakeFrames()
    llm = FakeLLM([sample_notes().model_dump()])
    services = make_services(redis, make_settings(frame_selection="transcript"), llm=llm, frames=frames)
    await store.create(_job())
    ctx = make_context(store, services, _job())
    await transcribe.run(ctx)
    await summarize.run(ctx)
    assert frames.scans == 0


async def test_rejected_selection_request_falls_back_immediately(store, redis):
    rejected = FatalError(ErrorCode.LLM_REQUEST_REJECTED, "request too large")
    services = await _prepare(store, redis, [rejected])
    result = await extract_frames.run(make_context(store, services, _job(), attempt=1))
    assert services.frames.calls == [30, 150]
    assert result.fields["warnings"][0].code == "VISUAL_SELECTION_UNAVAILABLE"


async def test_scan_failure_retries_the_stage(store, redis):
    from notetaker.services.frames import FrameCaptureError

    services = await _prepare(store, redis, [_VISUAL_PLAN], frames=FakeFrames(scan_error=FrameCaptureError("403")))
    try:
        await extract_frames.run(make_context(store, services, _job(), attempt=1))
    except RetryableError as exc:
        assert exc.code == ErrorCode.YOUTUBE_FAILED
    else:
        raise AssertionError("a failed scan should retry the stage")


async def test_scan_runs_during_summarize_and_is_reused(store, redis):
    frames = FakeFrames()
    services = await _prepare(store, redis, [_VISUAL_PLAN], frames=frames)
    assert frames.scans == 1 and await store.load_scan("job-1")  # cached while the notes were written
    await extract_frames.run(make_context(store, services, _job()))
    assert frames.scans == 1 and frames.calls == [120, 300]


async def test_scan_retries_with_a_fresh_stream_before_failing_the_stage(store, redis):
    # The first scan hits a bad stream URL; the retry on a freshly resolved one succeeds.
    frames = FakeFrames(scan_failures=1)
    services = await _prepare(store, redis, [_VISUAL_PLAN], frames=frames)
    assert frames.scans == 2
    await extract_frames.run(make_context(store, services, _job()))
    assert frames.calls == [120, 300]


async def test_failed_early_scan_does_not_fail_the_notes(store, redis):
    from notetaker.services.frames import FrameCaptureError

    services = await _prepare(store, redis, [_VISUAL_PLAN], frames=FakeFrames(scan_error=FrameCaptureError("403")))
    assert await store.load_artifact("job-1", "notes", Notes) is not None


async def test_empty_visual_plan_means_no_frames(store, redis):
    services = await _prepare(store, redis, [{"states": [], "frames": []}])
    result = await extract_frames.run(make_context(store, services, _job()))
    assert services.frames.calls == []
    assert result.fields["warnings"] == []


def test_validate_visual_frames_snaps_dedupes_and_drops():
    scanned = [0, 5, 10, 15, 20]
    frames = [
        FrameSuggestion(timestamp_sec=11, caption="near 10"),
        FrameSuggestion(timestamp_sec=10, caption="duplicate of 10"),
        FrameSuggestion(timestamp_sec=99, caption="never shown to the model"),
        FrameSuggestion(timestamp_sec=20, caption="   "),
        FrameSuggestion(timestamp_sec=0, caption="first"),
    ]
    kept = validate_visual_frames(frames, scanned, max_frames=10)
    assert [(f.timestamp_sec, f.caption) for f in kept] == [(0, "first"), (10, "near 10")]


def test_validate_visual_frames_caps_evenly():
    scanned = list(range(0, 100, 5))
    frames = [FrameSuggestion(timestamp_sec=t, caption="c") for t in scanned]
    kept = validate_visual_frames(frames, scanned, max_frames=3)
    assert [f.timestamp_sec for f in kept] == [0, 50, 95]


def test_scan_interval_respects_image_budget():
    assert scan_interval(600, 5, 240) == 5
    assert scan_interval(1200, 5, 120) == 10


def test_label_frame_keeps_size_and_is_only_used_for_the_prompt():
    original = Image.new("RGB", (640, 360), "white")
    buf = io.BytesIO()
    original.save(buf, "JPEG")
    labelled = label_frame(buf.getvalue(), 491)
    with Image.open(io.BytesIO(labelled)) as img:
        assert img.size == (640, 360)
        assert img.getpixel((2, 2))[0] < 50  # dark label box in the corner
    meta = VideoMeta(video_id="v", title="T", duration_sec=600)
    parts = build_selection_prompt(
        [ScanFrame(491, sample_jpeg())], sample_notes(), meta, interval_sec=5, max_frames=8, language="English"
    )
    assert parts[1] == "t=491s (08:11):" and isinstance(parts[2], bytes)


def test_parse_showinfo_times():
    stderr = (
        "[Parsed_showinfo_1 @ 0x55] n:   0 pts:      0 pts_time:0       duration:512\n"
        "frame=    1 fps=0.0 q=4.0 size=N/A\n"
        "[Parsed_showinfo_1 @ 0x55] n:   1 pts: 76800 pts_time:5.005   duration:512\n"
    )
    assert parse_showinfo_times(stderr) == [0.0, 5.005]
