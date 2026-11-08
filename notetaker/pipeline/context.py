"""What a stage handler gets to work with, and what it hands back to the worker."""
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from notetaker.config import Settings
from notetaker.domain.models import Job
from notetaker.domain.status import JobStatus
from notetaker.infra.job_store import JobStore
from notetaker.infra.lease import JobLease
from notetaker.infra.rate_limiter import ServiceLimits
from notetaker.services.frames import FrameGrabber
from notetaker.services.llm import LLMClient
from notetaker.services.notion import NotionClient
from notetaker.services.transcripts import TranscriptClient
from notetaker.services.youtube import YouTubeClient


@dataclass
class Services:
    youtube: YouTubeClient
    transcripts: TranscriptClient
    frames: FrameGrabber
    llm: LLMClient
    limits: ServiceLimits
    http: httpx.AsyncClient
    settings: Settings

    def notion(self, token: str) -> NotionClient:
        return NotionClient(token, self.settings, self.limits, self.http)


@dataclass
class StageResult:
    """Fields to write on the job when the worker advances it past this stage.

    `final_status` is only set by the last stage (COMPLETED vs COMPLETED_WITH_WARNINGS).
    """

    fields: dict = field(default_factory=dict)
    final_status: JobStatus | None = None


@dataclass
class StageContext:
    job: Job
    store: JobStore
    services: Services
    settings: Settings
    lease: JobLease
    attempt: int
    decrypt_token: Callable[[], str]

    @property
    def job_id(self) -> str:
        return self.job.job_id

    @property
    def is_last_attempt(self) -> bool:
        return self.attempt >= self.settings.max_attempts

    async def report(self, detail: str | None, progress: int | None = None) -> None:
        """Publish a human-readable sub-step (and optionally progress) for the popup."""
        fields: dict = {"detail": detail}
        if progress is not None:
            fields["progress"] = progress
        await self.store.update(self.job_id, **fields)

    def rate_limit_reporter(self, what: str):
        async def on_wait(seconds: float) -> None:
            await self.report(f"Waiting for {what} capacity (shared rate limit), about {seconds:.0f}s")

        return on_wait
