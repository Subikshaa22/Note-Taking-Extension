"""Request and response bodies of the public REST API (spec §5)."""
from pydantic import BaseModel, Field

from notetaker.domain.models import Job, JobError, JobResult, JobWarning, RetryInfo
from notetaker.domain.status import JobStatus


class SubmitJobRequest(BaseModel):
    youtube_url: str = Field(max_length=2048)
    notion_token: str = Field(min_length=1, max_length=1024)
    notion_parent_id: str | None = Field(default=None, max_length=2048)


class SubmitJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    video_id: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: int
    video_id: str
    video_title: str | None
    duration_sec: int | None
    warnings: list[JobWarning]
    error: JobError | None
    result: JobResult | None
    # Not in the spec's example, but needed for a good client experience: what the job is
    # doing right now, and when it will retry if it is waiting out a backoff.
    detail: str | None
    retry: RetryInfo | None
    # Which worker last claimed the job and how many attempts the current stage has used;
    # makes crash recovery visible (the id changes when another worker takes over).
    worker_id: str | None
    attempt: int
    created_at: str
    updated_at: str
    history: list[dict] | None = None

    @classmethod
    def from_job(cls, job: Job, history: list[dict] | None = None) -> "JobStatusResponse":
        return cls(**job.model_dump(include=set(cls.model_fields) - {"history"}), history=history)


class JobResultsResponse(BaseModel):
    status: JobStatus
    result: JobResult | None
    warnings: list[JobWarning]
    error: JobError | None


class OAuthConfigResponse(BaseModel):
    enabled: bool
    client_id: str | None
    authorize_url: str


class OAuthTokenRequest(BaseModel):
    code: str = Field(min_length=1, max_length=2048)
    redirect_uri: str = Field(min_length=1, max_length=2048)


class OAuthTokenResponse(BaseModel):
    access_token: str
    workspace_id: str | None = None
    workspace_name: str | None = None
    workspace_icon: str | None = None
    bot_id: str | None = None
