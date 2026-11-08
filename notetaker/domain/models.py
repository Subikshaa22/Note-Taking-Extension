"""Data models shared by the gateway, the workers and the pipeline stages."""
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from notetaker.domain.status import JobStatus


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def format_timestamp(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


# ---- Pipeline artifacts ------------------------------------------------------------


class VideoMeta(BaseModel):
    video_id: str
    title: str
    duration_sec: int
    channel: str | None = None


class TranscriptSegment(BaseModel):
    start: float
    duration: float = 0.0
    text: str


class Transcript(BaseModel):
    language_code: str
    is_generated: bool
    segments: list[TranscriptSegment]

    @property
    def end_sec(self) -> int:
        if not self.segments:
            return 0
        last = self.segments[-1]
        return int(last.start + last.duration)


class Section(BaseModel):
    heading: str = Field(description="Short heading for this part of the video")
    start_sec: int = Field(description="Second in the video where this part starts")
    bullets: list[str] = Field(description="Concise study-note bullet points for this part")


class FrameSuggestion(BaseModel):
    timestamp_sec: int = Field(description="Second in the video where the visual is on screen")
    caption: str = Field(description="What is shown on screen and why it was flagged as important")


class Notes(BaseModel):
    title: str
    summary: str = ""
    key_takeaways: list[str] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    frames: list[FrameSuggestion] = Field(default_factory=list)


# ---- Job state exposed through the API --------------------------------------------


class JobWarning(BaseModel):
    code: str
    message: str
    timestamp_sec: int | None = None


class JobError(BaseModel):
    code: str
    message: str
    stage: str | None = None


class JobResult(BaseModel):
    notion_page_url: str
    notion_page_id: str
    frames_requested: int
    frames_published: int


class RetryInfo(BaseModel):
    """Present while a job is waiting in a backoff queue after a transient failure."""

    attempt: int
    max_attempts: int
    next_attempt_at: str
    delay_sec: int
    reason_code: str
    reason: str


class Job(BaseModel):
    job_id: str
    video_id: str
    youtube_url: str
    notion_parent_id: str | None = None
    status: JobStatus = JobStatus.QUEUED
    progress: int = 0
    detail: str | None = None
    video_title: str | None = None
    duration_sec: int | None = None
    attempt: int = 0
    warnings: list[JobWarning] = Field(default_factory=list)
    error: JobError | None = None
    result: JobResult | None = None
    retry: RetryInfo | None = None
    worker_id: str | None = None
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
