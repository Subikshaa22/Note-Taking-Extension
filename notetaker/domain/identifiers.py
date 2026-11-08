"""Parsing of user-supplied identifiers: YouTube URLs and Notion page/database ids."""
import re
from urllib.parse import parse_qs, urlparse

from notetaker.domain.errors import ErrorCode, FatalError

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_YOUTUBE_HOSTS = {"youtube.com", "music.youtube.com", "youtube-nocookie.com"}
_PATH_PREFIXES = ("/shorts/", "/embed/", "/live/", "/v/")
_NOTION_HEX_ID = re.compile(r"([0-9a-f]{32})(?:[?#/]|$)")
_NOTION_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def parse_video_id(url: str) -> str:
    """Extract the 11-character video id from any common YouTube URL form."""
    if not isinstance(url, str) or not url.strip():
        raise FatalError(ErrorCode.INVALID_URL, "youtube_url is required.")
    raw = url.strip()
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise FatalError(ErrorCode.INVALID_URL, "Not a valid YouTube video URL.")
    host = (parsed.hostname or "").lower()
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)

    video_id = None
    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif parsed.path.startswith(_PATH_PREFIXES):
            parts = parsed.path.split("/")
            video_id = parts[2] if len(parts) > 2 else None

    if not video_id or not _VIDEO_ID.fullmatch(video_id):
        raise FatalError(ErrorCode.INVALID_URL, "Not a valid YouTube video URL.")
    return video_id


def canonical_video_url(video_id: str, at_sec: int | None = None) -> str:
    url = f"https://www.youtube.com/watch?v={video_id}"
    return f"{url}&t={int(at_sec)}s" if at_sec is not None else url


def parse_notion_id(value: str) -> str:
    """Accept a Notion page/database URL or id (dashed or not) and return the dashed UUID."""
    if not isinstance(value, str) or not value.strip():
        raise FatalError(ErrorCode.INVALID_NOTION_PARENT_ID, "Notion parent id is empty.")
    raw = value.strip().lower()
    if _NOTION_UUID.match(raw):
        return raw
    compact = raw.replace("-", "")
    match = _NOTION_HEX_ID.search(compact) if "://" in raw or "/" in raw else None
    if match is None and re.fullmatch(r"[0-9a-f]{32}", compact):
        hex_id = compact
    elif match is not None:
        hex_id = match.group(1)
    else:
        raise FatalError(
            ErrorCode.INVALID_NOTION_PARENT_ID,
            "notion_parent_id must be a Notion page/database URL or a 32-character id.",
        )
    return f"{hex_id[:8]}-{hex_id[8:12]}-{hex_id[12:16]}-{hex_id[16:20]}-{hex_id[20:]}"
