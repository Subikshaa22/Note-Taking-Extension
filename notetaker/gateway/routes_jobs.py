"""POST /jobs, GET /jobs/{id}, GET /jobs/{id}/results."""
import hashlib
import logging
import uuid

from fastapi import APIRouter, Header, Query, Request, Response

from notetaker.domain.errors import ErrorCode
from notetaker.domain.identifiers import canonical_video_url, parse_notion_id, parse_video_id
from notetaker.domain.models import Job, JobError, format_timestamp
from notetaker.domain.status import JobStatus, Stage
from notetaker.gateway.errors import ApiError
from notetaker.gateway.length_check import quick_duration
from notetaker.gateway.schemas import JobResultsResponse, JobStatusResponse, SubmitJobRequest, SubmitJobResponse
from notetaker.infra.broker import Envelope
from notetaker.infra.video_cache import VideoCache

log = logging.getLogger(__name__)
router = APIRouter()


async def _reject_if_too_long(request: Request, video_id: str) -> None:
    """Constraint A at submit time: an over-long video is refused before it is ever queued."""
    settings = request.app.state.settings
    if settings.length_precheck_timeout_sec <= 0:
        return
    cache = VideoCache(request.app.state.store.redis, settings)
    duration = await quick_duration(request.app.state.http, cache, video_id, settings.length_precheck_timeout_sec)
    limit = settings.max_video_duration_sec
    if duration and duration > limit:
        log.info("rejected video %s at submit: %ss is over the %ss limit", video_id, duration, limit)
        raise ApiError(
            400,
            ErrorCode.VIDEO_TOO_LONG,
            f"This video is {format_timestamp(duration)} long; the limit is {format_timestamp(limit)}. "
            "Choose a shorter video.",
        )


def _active_key(video_id: str, parent_id: str | None, notion_token: str) -> str:
    """Same video, same Notion account and same destination -> same job while it runs."""
    account = hashlib.sha256(notion_token.encode()).hexdigest()[:24]
    return f"{video_id}:{parent_id or 'default'}:{account}"


async def _load_job(request: Request, job_id: str) -> Job:
    job = await request.app.state.store.get(job_id)
    if job is None:
        raise ApiError(404, ErrorCode.JOB_NOT_FOUND, f"No job with id {job_id}.")
    return job


@router.post("/jobs", status_code=202, response_model=SubmitJobResponse)
async def submit_job(
    body: SubmitJobRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, max_length=200),
):
    """Validate, record and enqueue a job. No pipeline work happens in this request."""
    state = request.app.state
    video_id = parse_video_id(body.youtube_url)
    parent_id = parse_notion_id(body.notion_parent_id) if body.notion_parent_id else None
    await _reject_if_too_long(request, video_id)
    job_id = str(uuid.uuid4())

    if idempotency_key:
        # A double-clicked "Take Notes" button (same key) maps to the same job.
        existing_id = await state.store.claim_idempotency_key(idempotency_key, job_id, state.settings.idempotency_ttl_sec)
        if existing_id:
            existing = await state.store.get(existing_id)
            if existing is not None:
                response.headers["Location"] = f"/jobs/{existing.job_id}"
                return SubmitJobResponse(job_id=existing.job_id, status=existing.status, video_id=existing.video_id)

    # Repeated clicks on a video that is already being processed join the running job.
    active_key = _active_key(video_id, parent_id, body.notion_token)
    running_id = await state.store.claim_active(active_key, job_id, state.settings.job_ttl_sec)
    if running_id:
        running = await state.store.get(running_id)
        if running is not None:
            if idempotency_key:
                await state.store.bind_idempotency_key(idempotency_key, running_id, state.settings.idempotency_ttl_sec)
            log.info("video %s is already being processed by job %s; not starting another", video_id, running_id)
            response.headers["Location"] = f"/jobs/{running_id}"
            return SubmitJobResponse(job_id=running_id, status=running.status, video_id=video_id, already_running=True)

    job = Job(job_id=job_id, video_id=video_id, youtube_url=canonical_video_url(video_id), notion_parent_id=parent_id)
    await state.store.create(job)
    envelope = Envelope(job_id=job_id, stage=Stage.TRANSCRIBE, notion_token=state.cipher.encrypt(body.notion_token))
    try:
        await state.broker.publish_stage(envelope)
    except Exception as exc:
        log.exception("could not enqueue job %s", job_id)
        error = JobError(code=ErrorCode.BROKER_UNAVAILABLE, message="The job queue is unavailable.")
        await state.store.transition(job_id, JobStatus.FAILED, error=error)
        if idempotency_key:
            await state.store.release_idempotency_key(idempotency_key)
        await state.store.release_active(active_key, job_id)
        raise ApiError(503, ErrorCode.BROKER_UNAVAILABLE, "The job queue is unavailable; try again shortly.") from exc

    log.info("job %s queued for video %s", job_id, video_id)
    response.headers["Location"] = f"/jobs/{job_id}"
    return SubmitJobResponse(job_id=job_id, status=JobStatus.QUEUED, video_id=video_id)


@router.get("/jobs/{job_id}", response_model=JobStatusResponse, response_model_exclude_none=False)
async def get_job(job_id: str, request: Request, include: str | None = Query(default=None)):
    """Read-only status poll. `?include=history` adds the job's audit trail."""
    job = await _load_job(request, job_id)
    history = await request.app.state.store.events(job_id) if include == "history" else None
    return JobStatusResponse.from_job(job, history)


@router.get("/jobs/{job_id}/results", response_model=JobResultsResponse)
async def get_results(job_id: str, request: Request):
    job = await _load_job(request, job_id)
    if not job.status.is_terminal:
        raise ApiError(409, ErrorCode.JOB_NOT_FINISHED, f"Job is still {job.status.value}; poll GET /jobs/{job_id}.")
    return JobResultsResponse(status=job.status, result=job.result, warnings=job.warnings, error=job.error)
