"""Runtime configuration, read from environment variables (or a local .env file).

Every setting maps to an upper-case env var of the same name, e.g. `max_attempts` ->
MAX_ATTEMPTS. List settings take comma-separated values (RETRY_DELAYS_SEC=10,30,90).
"""
from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value):
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Infrastructure -----------------------------------------------------------
    amqp_url: str = "amqp://guest:guest@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"
    # Fernet key shared by gateway and workers; encrypts the Notion token inside queue messages.
    token_encryption_key: str = ""

    # --- Gateway ------------------------------------------------------------------
    cors_allow_origins: Annotated[list[str], NoDecode] = ["*"]
    idempotency_ttl_sec: int = 24 * 3600

    # --- Jobs, retries and the dead-letter queue -----------------------------------
    max_video_duration_sec: int = 20 * 60
    job_ttl_sec: int = 7 * 24 * 3600
    # Per-video cache (transcript, notes, frame choice, frame images) so the same video is fast
    # the second time. 0 turns it off (e.g. to demo the full pipeline on a video used before).
    video_cache_ttl_sec: int = 7 * 24 * 3600
    max_attempts: int = 5
    retry_delays_sec: Annotated[list[int], NoDecode] = [10, 30, 90]
    lease_ttl_sec: int = 10
    worker_concurrency: int = 1
    # RabbitMQ-side safety net against messages that crash every worker before they can
    # record an attempt (poison messages). Kept well above max_attempts.
    queue_delivery_limit: int = 20

    # --- LLM ------------------------------------------------------------------------
    gemini_api_key: str = ""
    llm_model: str = "gemini-3.5-flash"
    llm_fallback_model: str = ""
    # Also sent to Gemini as its server-side deadline (X-Server-Timeout): too low and Gemini
    # answers 504 "Deadline expired" on long transcripts. Notes for a 20-min video can take ~2 min.
    llm_timeout_sec: int = 300
    llm_temperature: float = 0.2
    # Transcripts longer than this many characters are split into chunks (map/reduce).
    llm_chunk_chars: int = 60_000
    notes_language: str = "English"
    max_frames: int = 15
    min_frame_gap_sec: int = 10

    # --- Global rate limits (shared by all workers through Redis) ----------------------
    llm_rpm: int = 10
    llm_tpm: int = 250_000
    notion_rps: int = 3
    youtube_rpm: int = 60

    # --- YouTube --------------------------------------------------------------------
    transcript_languages: Annotated[list[str], NoDecode] = ["en", "en-US", "en-GB"]
    ytdlp_cookies_file: str = ""
    frame_max_height: int = 720
    frame_timeout_sec: int = 45
    frame_concurrency: int = 3
    # How frame timestamps are chosen. "visual": scan the video and let the vision model pick,
    # for every state of the board/slides, the moment where the content is most complete and
    # least blocked by the presenter. "transcript": trust the transcript-based suggestions only.
    frame_selection: str = "visual"
    frame_scan_interval_sec: int = 10
    frame_scan_max_images: int = 120
    frame_scan_height: int = 360
    frame_scan_timeout_sec: int = 600
    # Time limit for the vision call that picks the frames. It sends up to FRAME_SCAN_MAX_IMAGES
    # images, so it gets longer than a text call, but an overloaded model should not hold the job.
    frame_selection_timeout_sec: int = 240
    # How many stage attempts may try the vision call when it fails for a reason other than a
    # rate limit (timeout, 503 overload). After that the transcript-based frames are used.
    # 1 = fall back on the first failure (fastest); 429s are always re-queued with backoff.
    visual_selection_attempts: int = 1

    # --- Notion ---------------------------------------------------------------------
    notion_client_id: str = ""
    notion_client_secret: str = ""
    notion_default_parent_id: str = ""
    notion_api_base: str = "https://api.notion.com/v1"
    notion_version: str = "2022-06-28"
    notion_timeout_sec: int = 30

    # --- Fault injection for demos (probability 0..1 per call; 0 = off) -------------------
    fault_llm_429_rate: float = 0.0
    fault_notion_429_rate: float = 0.0
    fault_youtube_429_rate: float = 0.0
    fault_frame_failure_rate: float = 0.0

    @field_validator("cors_allow_origins", "transcript_languages", "retry_delays_sec", mode="before")
    @classmethod
    def _csv(cls, value):
        return _split_csv(value)

    @field_validator("retry_delays_sec")
    @classmethod
    def _positive_delays(cls, value: list[int]) -> list[int]:
        if not value or any(d <= 0 for d in value):
            raise ValueError("RETRY_DELAYS_SEC needs at least one positive delay")
        return value

    @field_validator("frame_selection")
    @classmethod
    def _frame_selection(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ("visual", "transcript"):
            raise ValueError("FRAME_SELECTION must be 'visual' or 'transcript'")
        return value

    @field_validator(
        "max_attempts", "worker_concurrency", "max_frames", "llm_rpm", "notion_rps", "youtube_rpm",
        "frame_scan_interval_sec", "frame_scan_max_images", "frame_selection_timeout_sec",
        "visual_selection_attempts",
    )
    @classmethod
    def _at_least_one(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value

    def retry_delay_for(self, attempt: int) -> int:
        """Backoff before attempt `attempt + 1`; the last delay repeats once the list runs out."""
        idx = min(max(attempt, 1), len(self.retry_delays_sec)) - 1
        return self.retry_delays_sec[idx]

    @property
    def notion_oauth_enabled(self) -> bool:
        return bool(self.notion_client_id and self.notion_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
