"""Error/warning codes and the exception hierarchy that drives retry decisions.

Every failure raised inside a stage is one of:
  * FatalError       - deterministic, retrying cannot help -> job FAILED
  * RetryableError   - transient; re-queued with backoff until the attempt budget runs out,
                       then the job is DEAD_LETTERED
  * RateLimitedError - a RetryableError caused by an external 429 / throttling signal
"""
from enum import StrEnum


class ErrorCode(StrEnum):
    # Request validation (gateway)
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_URL = "INVALID_URL"
    INVALID_NOTION_PARENT_ID = "INVALID_NOTION_PARENT_ID"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_FINISHED = "JOB_NOT_FINISHED"
    BROKER_UNAVAILABLE = "BROKER_UNAVAILABLE"
    OAUTH_NOT_CONFIGURED = "OAUTH_NOT_CONFIGURED"
    OAUTH_EXCHANGE_FAILED = "OAUTH_EXCHANGE_FAILED"

    # Video / transcript
    VIDEO_TOO_LONG = "VIDEO_TOO_LONG"
    LIVE_NOT_SUPPORTED = "LIVE_NOT_SUPPORTED"
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"
    TRANSCRIPT_UNAVAILABLE = "TRANSCRIPT_UNAVAILABLE"
    YOUTUBE_FAILED = "YOUTUBE_FAILED"

    # LLM
    LLM_AUTH_FAILED = "LLM_AUTH_FAILED"
    LLM_REQUEST_REJECTED = "LLM_REQUEST_REJECTED"
    LLM_BAD_OUTPUT = "LLM_BAD_OUTPUT"
    LLM_FAILED = "LLM_FAILED"
    LLM_OVERLOADED = "LLM_OVERLOADED"
    LLM_QUOTA_EXHAUSTED = "LLM_QUOTA_EXHAUSTED"

    # Notion
    NOTION_UNAUTHORIZED = "NOTION_UNAUTHORIZED"
    NOTION_FORBIDDEN = "NOTION_FORBIDDEN"
    NOTION_PARENT_NOT_FOUND = "NOTION_PARENT_NOT_FOUND"
    NOTION_NO_DESTINATION = "NOTION_NO_DESTINATION"
    NOTION_REQUEST_REJECTED = "NOTION_REQUEST_REJECTED"
    NOTION_FAILED = "NOTION_FAILED"

    # Generic
    RATE_LIMITED = "RATE_LIMITED"
    CONFIG_ERROR = "CONFIG_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class WarningCode(StrEnum):
    FRAME_EXTRACTION_FAILED = "FRAME_EXTRACTION_FAILED"
    FRAME_UPLOAD_FAILED = "FRAME_UPLOAD_FAILED"
    VISUAL_SELECTION_UNAVAILABLE = "VISUAL_SELECTION_UNAVAILABLE"


class PipelineError(Exception):
    """Base class: a stable machine-readable `code` plus a message safe to show to users."""

    retryable = False

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class FatalError(PipelineError):
    retryable = False


class RetryableError(PipelineError):
    retryable = True


class RateLimitedError(RetryableError):
    """An external service throttled us. `retry_after_sec` is the server's hint, if any."""

    def __init__(self, service: str, message: str, retry_after_sec: float | None = None):
        super().__init__(ErrorCode.RATE_LIMITED, message)
        self.service = service
        self.retry_after_sec = retry_after_sec


class OverloadedError(RetryableError):
    """The provider is up but temporarily overloaded (HTTP 5xx). Such spells often last minutes,
    so these retries wait longer than ordinary ones (OVERLOAD_RETRY_DELAYS_SEC)."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(code, message)


class LeaseLostError(Exception):
    """This worker no longer owns the job; it must stop before causing side effects."""
