import io

import httpx
import pytest
from google.genai import errors as genai_errors
from PIL import Image

from notetaker.config import Settings
from notetaker.domain.errors import ErrorCode, FatalError, RateLimitedError, RetryableError
from notetaker.infra.rate_limiter import RateLimiter, ServiceLimits
from notetaker.services.frames import FrameCaptureError, check_frame
from notetaker.services.llm import GeminiClient, estimate_tokens
from notetaker.services.notion import NotionClient, NotionNotFound
from notetaker.services.youtube import classify_ytdlp_error


@pytest.fixture
def limits(redis):
    return ServiceLimits(RateLimiter(redis), llm_rpm=100, llm_tpm=10**7, notion_rps=100, youtube_rpm=100)


# ---- YouTube --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", ErrorCode.VIDEO_UNAVAILABLE),
        ("ERROR: [youtube] abc: Video unavailable", ErrorCode.VIDEO_UNAVAILABLE),
        ("ERROR: Sign in to confirm you're not a bot", ErrorCode.RATE_LIMITED),
        ("HTTP Error 429: Too Many Requests", ErrorCode.RATE_LIMITED),
        ("ERROR: Unable to extract player response", ErrorCode.YOUTUBE_FAILED),
    ],
)
def test_classify_ytdlp_error(message, expected):
    error = classify_ytdlp_error(message)
    assert isinstance(error, RetryableError)
    assert error.code == expected


# ---- Frames ---------------------------------------------------------------------------


def _jpeg(pattern: bool) -> bytes:
    img = Image.new("RGB", (64, 36), "black")
    if pattern:
        for x in range(0, 64, 4):
            for y in range(36):
                img.putpixel((x, y), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG")
    return buf.getvalue()


def test_check_frame_accepts_real_image():
    assert check_frame(_jpeg(pattern=True)) == (64, 36)


@pytest.mark.parametrize("data", [b"", b"not an image", _jpeg(pattern=False)])
def test_check_frame_rejects_empty_corrupt_or_blank(data):
    with pytest.raises(FrameCaptureError):
        check_frame(data)


# ---- LLM ------------------------------------------------------------------------------


def test_estimate_tokens_is_conservative():
    assert estimate_tokens("x" * 350) == 100


async def test_gemini_error_classification(limits, redis):
    client = GeminiClient(Settings(_env_file=None, gemini_api_key="k"), limits)
    rate = await client._classify(
        genai_errors.APIError(
            429,
            {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota", "details": [{"retryDelay": "37s"}]}},
        ),
        "m",
    )
    assert isinstance(rate, RateLimitedError) and rate.retry_after_sec == 37
    assert await redis.pttl("ratelimit:cooldown:llm:m") > 30_000

    auth = await client._classify(genai_errors.APIError(400, {"error": {"message": "API key not valid."}}), "m")
    assert isinstance(auth, FatalError) and auth.code == ErrorCode.LLM_AUTH_FAILED

    server = await client._classify(genai_errors.APIError(503, {"error": {"message": "overloaded"}}), "m")
    assert isinstance(server, RetryableError) and server.code == ErrorCode.LLM_FAILED


def test_gemini_requires_api_key(limits):
    with pytest.raises(FatalError) as exc:
        GeminiClient(Settings(_env_file=None, gemini_api_key=""), limits)
    assert exc.value.code == ErrorCode.CONFIG_ERROR


# ---- Notion ---------------------------------------------------------------------------


def _notion(limits, handler) -> NotionClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NotionClient("secret", Settings(_env_file=None), limits, http)


async def test_notion_sends_auth_and_version_headers(limits):
    seen = {}

    def handler(request: httpx.Request):
        seen.update(request.headers)
        return httpx.Response(200, json={"id": "p1", "url": "https://www.notion.so/p1"})

    page = await _notion(limits, handler).retrieve_page("p1")
    assert page["id"] == "p1"
    assert seen["authorization"] == "Bearer secret"
    assert seen["notion-version"] == "2022-06-28"


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (429, {"Retry-After": "2"}, RateLimitedError),
        (401, {}, FatalError),
        (403, {}, FatalError),
        (404, {}, NotionNotFound),
        (409, {}, RetryableError),
        (502, {}, RetryableError),
        (400, {}, FatalError),
    ],
)
async def test_notion_error_mapping(limits, status, headers, expected):
    client = _notion(limits, lambda r: httpx.Response(status, headers=headers, json={"message": "boom"}))
    with pytest.raises(expected):
        await client.retrieve_page("p1")


async def test_notion_upload_image_two_step(limits):
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/send"):
            assert b"frame.jpg" in request.content
            return httpx.Response(200, json={"id": "u1", "status": "uploaded"})
        return httpx.Response(200, json={"id": "u1", "status": "pending"})

    assert await _notion(limits, handler).upload_image("frame.jpg", b"jpeg") == "u1"
    assert calls == [("POST", "/v1/file_uploads"), ("POST", "/v1/file_uploads/u1/send")]


# ---- Fault injection -------------------------------------------------------------------


async def test_injected_frame_failure():
    from notetaker.services.frames import FrameGrabber
    from notetaker.services.youtube import StreamInfo

    grabber = FrameGrabber(5, ffmpeg_bin="does-not-exist", failure_rate=1.0)
    with pytest.raises(FrameCaptureError, match="simulated"):
        await grabber.grab(StreamInfo(url="u", http_headers={}, height=None), 10)


async def test_injected_notion_429(limits):
    settings = Settings(_env_file=None, fault_notion_429_rate=1.0)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    with pytest.raises(RateLimitedError):
        await NotionClient("t", settings, limits, http).retrieve_page("p")


async def test_injected_llm_429_goes_through_rate_limit_path(limits):
    client = GeminiClient(Settings(_env_file=None, gemini_api_key="k", fault_llm_429_rate=1.0), limits)
    with pytest.raises(RateLimitedError):
        await client.generate("prompt", Settings)
