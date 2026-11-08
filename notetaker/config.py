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
    llm_timeout_sec: int = 120
    llm_temperature: float = 0.2
    # Transcripts longer than this many characters are split into chunks (map/reduce).
    llm_chunk_chars: int = 60_000
    notes_language: str = "English"
    max_frames: int = 8
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

    # --- Notion ---------------------------------------------------------------------
    notion_client_id: str = ""
    notion_client_secret: str = ""
    notion_default_parent_id: str = ""
    notion_api_base: str = "https://api.notion.com/v1"
    notion_version: str = "2022-06-28"
    notion_timeout_sec: int = 30

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

    @field_validator("max_attempts", "worker_concurrency", "max_frames", "llm_rpm", "notion_rps", "youtube_rpm")
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
