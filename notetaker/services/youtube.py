"""YouTube access through yt-dlp: video metadata (duration, title) and direct stream URLs.

yt-dlp is synchronous, so calls run in a worker thread to keep the event loop (and the
RabbitMQ heartbeats it services) responsive.
"""
import asyncio
import logging
import time
from dataclasses import dataclass

import yt_dlp

from notetaker.config import Settings
from notetaker.domain.errors import ErrorCode, FatalError, RateLimitedError, RetryableError
from notetaker.domain.identifiers import canonical_video_url
from notetaker.domain.models import VideoMeta
from notetaker.infra.rate_limiter import ServiceLimits

log = logging.getLogger(__name__)

_UNAVAILABLE_MARKERS = (
    "private video",
    "video is private",
    "video unavailable",
    "video is unavailable",
    "this video has been removed",
    "this video is not available",
    "members-only",
    "join this channel",
    "confirm your age",
    "account associated with this video has been terminated",
)
_THROTTLE_MARKERS = ("http error 429", "too many requests", "not a bot", "rate-limit", "rate limit")


@dataclass(frozen=True)
class StreamInfo:
    url: str
    http_headers: dict[str, str]
    height: int | None


def classify_ytdlp_error(message: str) -> Exception:
    """Map a yt-dlp failure message onto the domain error hierarchy."""
    text = message.lower()
    if any(marker in text for marker in _THROTTLE_MARKERS):
        return RateLimitedError("youtube", "YouTube is throttling requests from this server.")
    if any(marker in text for marker in _UNAVAILABLE_MARKERS):
        # Treated as retryable on purpose: availability can be transient (region/age gates,
        # temporary takedowns), and a permanently private video then ends up in the DLQ
        # after the bounded number of attempts, as the spec describes.
        return RetryableError(ErrorCode.VIDEO_UNAVAILABLE, "The video is private, removed or otherwise unavailable.")
    return RetryableError(ErrorCode.YOUTUBE_FAILED, f"Could not read the video from YouTube: {message[:300]}")


class YouTubeClient:
    def __init__(self, settings: Settings, limits: ServiceLimits):
        self._settings = settings
        self._limits = limits

    def _options(self, **extra) -> dict:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 30,
            **extra,
        }
        if self._settings.ytdlp_cookies_file:
            opts["cookiefile"] = self._settings.ytdlp_cookies_file
        return opts

    async def _extract(self, video_id: str, **extra) -> dict:
        await self._limits.youtube()

        def run() -> dict:
            with yt_dlp.YoutubeDL(self._options(**extra)) as ydl:
                return ydl.extract_info(canonical_video_url(video_id), download=False)

        try:
            return await asyncio.to_thread(run)
        except yt_dlp.utils.DownloadError as exc:
            raise classify_ytdlp_error(str(exc)) from exc

    async def fetch_metadata(self, video_id: str) -> VideoMeta:
        info = await self._extract(video_id)
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise FatalError(ErrorCode.LIVE_NOT_SUPPORTED, "Live streams and premieres are not supported.")
        return VideoMeta(
            video_id=video_id,
            title=info.get("title") or video_id,
            duration_sec=int(info.get("duration") or 0),
            channel=info.get("channel") or info.get("uploader"),
        )

    async def resolve_stream(self, video_id: str) -> StreamInfo:
        """Pick a progressive/HTTP video stream that ffmpeg can seek into with range requests."""
        h = self._settings.frame_max_height
        selector = (
            f"bv*[height<={h}][vcodec^=avc1][protocol=https]"
            f"/bv*[height<={h}][protocol=https]"
            f"/b[height<={h}][protocol=https]"
            f"/bv*[protocol=https]/b"
        )
        info = await self._extract(video_id, format=selector)
        fmt = info if info.get("url") else (info.get("requested_formats") or [{}])[0]
        if not fmt.get("url"):
            raise RetryableError(ErrorCode.YOUTUBE_FAILED, "No downloadable video stream was found.")
        # Some player clients hand out URLs that answer 403 until `available_at` (a few
        # seconds after extraction); yt-dlp's own downloader sleeps for the same reason.
        available_at = fmt.get("available_at") or info.get("available_at")
        if available_at:
            await asyncio.sleep(max(0.0, float(available_at) - time.time()) + 0.5)
        return StreamInfo(url=fmt["url"], http_headers=dict(fmt.get("http_headers") or {}), height=fmt.get("height"))
