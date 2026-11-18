"""In-memory stand-ins for the external services, shared by pipeline and worker tests."""
import io
import json
import re
import uuid
from dataclasses import dataclass, field

import httpx
from PIL import Image

from notetaker.config import Settings
from notetaker.domain.models import Job, Notes, Transcript, TranscriptSegment, VideoMeta
from notetaker.infra.rate_limiter import RateLimiter, ServiceLimits
from notetaker.pipeline.context import Services, StageContext
from notetaker.services.frames import FrameCaptureError, ScanFrame
from notetaker.services.youtube import StreamInfo


def sample_jpeg() -> bytes:
    img = Image.new("RGB", (32, 18), "black")
    for x in range(0, 32, 3):
        img.putpixel((x, 5), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def sample_transcript(n: int = 20, step: int = 30) -> Transcript:
    return Transcript(
        language_code="en",
        is_generated=False,
        segments=[TranscriptSegment(start=i * step, duration=step, text=f"line {i}") for i in range(n)],
    )


class FakeLLM:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.prompts: list[str] = []

    async def generate(self, prompt, schema, on_wait=None):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return schema.model_validate(response)

    async def generate_from_parts(self, parts, schema, on_wait=None, timeout_sec=None, image_resolution="high"):
        self.image_resolution = image_resolution
        self.prompts.append(parts)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return schema.model_validate(response)


@dataclass
class FakeYouTube:
    meta: VideoMeta
    stream_error: Exception | None = None

    async def fetch_metadata(self, video_id):
        return self.meta

    async def resolve_stream(self, video_id, max_height=None):
        if self.stream_error:
            raise self.stream_error
        return StreamInfo(url="https://stream", http_headers={}, height=720)


@dataclass
class FakeTranscripts:
    transcript: Transcript
    calls: int = 0

    async def fetch(self, video_id):
        self.calls += 1
        return self.transcript


@dataclass
class FakeFrames:
    failing: set[int] = field(default_factory=set)
    calls: list[int] = field(default_factory=list)

    scans: int = 0
    scan_error: Exception | None = None

    async def grab(self, stream, timestamp_sec):
        self.calls.append(timestamp_sec)
        if timestamp_sec in self.failing:
            raise FrameCaptureError("decode error")
        return sample_jpeg()

    async def scan(self, stream, interval_sec, height, timeout_sec):
        self.scans += 1
        if self.scan_error:
            raise self.scan_error
        return [ScanFrame(ts, sample_jpeg()) for ts in range(0, 600, interval_sec)]


class FakeNotion:
    """Just enough of the Notion REST API for the publisher, served through httpx.MockTransport."""

    def __init__(self, parent_id: str = "parent-page"):
        self.parent_id = parent_id
        self.pages: dict[str, dict] = {}
        self.children: dict[str, list[dict]] = {parent_id: []}
        self.uploads: dict[str, str] = {}
        self.fail_next_create_after_commit = False
        self.calls: list[tuple[str, str]] = []
        # Where the shared parent page sits, and what /search returns (most recently edited first).
        self.parent_parent: dict = {"type": "workspace"}
        self.search_results: list[dict] | None = None

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/v1")
        self.calls.append((request.method, path))
        body = json.loads(request.content) if request.headers.get("content-type") == "application/json" else None

        if request.method == "GET" and (m := re.fullmatch(r"/pages/([\w-]+)", path)):
            pid = m.group(1)
            if pid == self.parent_id or pid in self.pages:
                parent = self.parent_parent if pid == self.parent_id else self.pages[pid]["parent"]
                return httpx.Response(200, json={"id": pid, "url": f"https://www.notion.so/{pid}", "parent": parent})
            return httpx.Response(404, json={"message": "not found"})
        if request.method == "POST" and path == "/search":
            results = self.search_results
            if results is None:
                results = [{"id": self.parent_id, "parent": self.parent_parent}]
            return httpx.Response(200, json={"results": results})
        if request.method == "POST" and path == "/file_uploads":
            uid = uuid.uuid4().hex
            self.uploads[uid] = "pending"
            return httpx.Response(200, json={"id": uid, "status": "pending"})
        if request.method == "POST" and (m := re.fullmatch(r"/file_uploads/(\w+)/send", path)):
            self.uploads[m.group(1)] = "uploaded"
            return httpx.Response(200, json={"id": m.group(1), "status": "uploaded"})
        if request.method == "POST" and path == "/pages":
            pid = uuid.uuid4().hex
            title = body["properties"]["title"][0]["text"]["content"]
            self.pages[pid] = {"title": title, "parent": body["parent"]}
            self.children[pid] = list(body["children"])
            self.children[self.parent_id].append({"id": pid, "type": "child_page", "child_page": {"title": title}})
            if self.fail_next_create_after_commit:
                self.fail_next_create_after_commit = False
                raise httpx.ReadTimeout("response lost after the page was created")
            return httpx.Response(200, json={"id": pid, "url": f"https://www.notion.so/{pid}"})
        if (m := re.fullmatch(r"/blocks/([\w-]+)/children", path)):
            bid = m.group(1)
            if request.method == "PATCH":
                self.children[bid].extend(body["children"])
                return httpx.Response(200, json={"results": []})
            blocks = [self._with_plain_text(b) for b in self.children.get(bid, [])]
            return httpx.Response(200, json={"results": blocks, "has_more": False})
        return httpx.Response(400, json={"message": f"unhandled {request.method} {path}"})

    @staticmethod
    def _with_plain_text(block: dict) -> dict:
        block = json.loads(json.dumps(block))
        payload = block.get(block.get("type"), {})
        for rt in payload.get("rich_text", []):
            rt["plain_text"] = rt["text"]["content"]
        return block


def make_settings(**overrides) -> Settings:
    defaults = dict(
        _env_file=None, max_attempts=3, retry_delays_sec=[1, 2], gemini_api_key="k", frame_selection="transcript"
    )
    return Settings(**{**defaults, **overrides})


def make_services(redis, settings, *, llm=None, youtube=None, transcripts=None, frames=None, notion=None) -> Services:
    limits = ServiceLimits(RateLimiter(redis), llm_rpm=1000, llm_tpm=10**8, notion_rps=1000, youtube_rpm=1000)
    meta = VideoMeta(video_id="dQw4w9WgXcQ", title="Demo video", duration_sec=600)
    return Services(
        youtube=youtube or FakeYouTube(meta),
        transcripts=transcripts or FakeTranscripts(sample_transcript()),
        frames=frames or FakeFrames(),
        llm=llm or FakeLLM([]),
        limits=limits,
        http=httpx.AsyncClient(transport=(notion or FakeNotion()).transport()),
        settings=settings,
    )


class NoopLease:
    async def ensure_held(self):
        return None


def make_context(store, services, job: Job, attempt: int = 1) -> StageContext:
    return StageContext(
        job=job,
        store=store,
        services=services,
        settings=services.settings,
        lease=NoopLease(),
        attempt=attempt,
        decrypt_token=lambda: "secret",
    )


def sample_notes() -> Notes:
    return Notes.model_validate(
        {
            "title": "Notes",
            "summary": "A summary.",
            "key_takeaways": ["one"],
            "sections": [
                {"heading": "Intro", "start_sec": 0, "bullets": ["a", "b"]},
                {"heading": "Diagram", "start_sec": 120, "bullets": ["c"]},
            ],
            "frames": [
                {"timestamp_sec": 30, "caption": "Title slide shows the agenda."},
                {"timestamp_sec": 150, "caption": "Architecture diagram of the system."},
            ],
        }
    )
