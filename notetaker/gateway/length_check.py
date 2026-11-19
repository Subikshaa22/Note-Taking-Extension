"""Fast video-length lookup so the gateway can reject over-long videos (Constraint A) at once.

The worker's transcribe stage stays the authoritative check; this only spares the user the
wait in the queue. Sources, cheapest first: the per-video cache a worker filled earlier, then
the length YouTube embeds in the watch page (about a second, no yt-dlp). Any failure returns
None and the job is accepted as before.
"""
import logging
import re

import httpx

from notetaker.infra.video_cache import VideoCache

log = logging.getLogger(__name__)

_LENGTH = re.compile(r'"videoDetails":\{"videoId":"([\w-]{11})".{0,600}?"lengthSeconds":"(\d+)"', re.S)
_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}


def parse_watch_page_length(html: str, video_id: str) -> int | None:
    match = _LENGTH.search(html)
    if not match or match.group(1) != video_id:
        return None
    return int(match.group(2)) or None


async def quick_duration(http: httpx.AsyncClient, cache: VideoCache, video_id: str, timeout_sec: float) -> int | None:
    cached = await cache.get(video_id, "meta")
    if cached and cached.get("duration_sec"):
        return int(cached["duration_sec"])
    try:
        response = await http.get(
            f"https://www.youtube.com/watch?v={video_id}", headers=_HEADERS, timeout=timeout_sec, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        log.info("quick length check for %s failed (%s); the worker will check it", video_id, exc)
        return None
    if response.status_code != 200:
        return None
    return parse_watch_page_length(response.text, video_id)
