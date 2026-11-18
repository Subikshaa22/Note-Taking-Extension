"""Per-video cache shared by all jobs, so submitting the same video again is fast.

Job artifacts belong to one job; this cache belongs to the video. It keeps what does not depend
on who submitted the video or where it is published: metadata, the transcript, the notes the
LLM wrote, the final frame choice (only when the vision model made it) and the captured frame
images. Keys include every setting that changes the content (language, frame budget, model,
frame mode...), so changing a setting never serves stale notes. Publishing always happens
again: each job still creates its own Notion page.

VIDEO_CACHE_TTL_SEC=0 turns the cache off.
"""
import base64
import hashlib
import json

from redis.asyncio import Redis

from notetaker.config import Settings


def _digest(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


class VideoCache:
    def __init__(self, redis: Redis, settings: Settings):
        self._redis = redis
        self._settings = settings
        self._ttl = settings.video_cache_ttl_sec

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    # ---- Keys: one per kind of content, varied by the settings that shape it ----------------

    def transcript_key(self) -> str:
        return f"transcript:{_digest(*self._settings.transcript_languages)}"

    def draft_notes_key(self) -> str:
        s = self._settings
        return f"notes:{_digest(s.llm_model, s.notes_language, s.max_frames, s.min_frame_gap_sec, s.llm_chunk_chars)}"

    def final_notes_key(self) -> str:
        s = self._settings
        variant = (s.frame_selection, s.frame_scan_interval_sec, s.frame_scan_max_images, s.frame_scan_height)
        return f"final:{_digest(self.draft_notes_key(), *variant)}"

    def _frame_key(self, video_id: str, timestamp_sec: int) -> str:
        return f"vcache:{video_id}:frame:{self._settings.frame_max_height}:{timestamp_sec}"

    # ---- JSON documents -------------------------------------------------------------------

    async def get(self, video_id: str, name: str) -> dict | None:
        if not self.enabled:
            return None
        raw = await self._redis.get(f"vcache:{video_id}:{name}")
        return json.loads(raw) if raw else None

    async def put(self, video_id: str, name: str, value: dict) -> None:
        if self.enabled:
            await self._redis.set(f"vcache:{video_id}:{name}", json.dumps(value), ex=self._ttl)

    # ---- Captured frames ------------------------------------------------------------------

    async def get_frame(self, video_id: str, timestamp_sec: int) -> bytes | None:
        if not self.enabled:
            return None
        encoded = await self._redis.get(self._frame_key(video_id, timestamp_sec))
        return base64.b64decode(encoded) if encoded else None

    async def put_frame(self, video_id: str, timestamp_sec: int, image: bytes) -> None:
        if self.enabled:
            await self._redis.set(
                self._frame_key(video_id, timestamp_sec), base64.b64encode(image).decode("ascii"), ex=self._ttl
            )
