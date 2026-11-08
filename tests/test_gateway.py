import httpx
import pytest
from cryptography.fernet import Fernet

from notetaker.domain.models import JobResult, JobWarning
from notetaker.domain.status import JobStatus
from notetaker.gateway.app import create_app
from notetaker.infra.crypto import TokenCipher
from tests.fakes import make_settings

KEY = Fernet.generate_key().decode()
URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class RecordingBroker:
    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    async def publish_stage(self, envelope):
        if self.fail:
            raise ConnectionError("broker down")
        self.published.append(envelope)

    async def queue_depths(self):
        return {"notes.jobs": len(self.published), "notes.dlq": 0}


@pytest.fixture
def broker():
    return RecordingBroker()


@pytest.fixture
async def client(store, broker):
    notion = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"access_token": "ntn_x", "workspace_name": "WS", "bot_id": "b"})
    )
    settings = make_settings(token_encryption_key=KEY, notion_client_id="cid", notion_client_secret="csecret")
    app = create_app(settings, store=store, broker=broker, http=httpx.AsyncClient(transport=notion))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _submit(client, **overrides):
    body = {"youtube_url": URL, "notion_token": "secret_abc", **overrides}
    return await client.post("/jobs", json=body)


async def test_submit_returns_202_with_location(client, broker, store):
    response = await _submit(client)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "QUEUED" and body["video_id"] == "dQw4w9WgXcQ"
    assert response.headers["location"] == f"/jobs/{body['job_id']}"

    envelope = broker.published[0]
    assert envelope.stage == "transcribe"
    assert "secret_abc" not in envelope.notion_token, "token must be encrypted in the queue"
    assert TokenCipher(KEY).decrypt(envelope.notion_token) == "secret_abc"
    assert (await store.get(body["job_id"])).status == JobStatus.QUEUED


@pytest.mark.parametrize(
    ("overrides", "status", "code"),
    [
        ({"youtube_url": "https://vimeo.com/1"}, 400, "INVALID_URL"),
        ({"notion_token": ""}, 422, "INVALID_REQUEST"),
        ({"notion_parent_id": "nope"}, 400, "INVALID_NOTION_PARENT_ID"),
    ],
)
async def test_submit_rejects_invalid_input(client, broker, overrides, status, code):
    response = await _submit(client, **overrides)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert set(response.json()["error"]) == {"code", "message"}
    assert not broker.published


async def test_submit_missing_body_fields(client):
    response = await client.post("/jobs", json={"youtube_url": URL})
    assert response.status_code == 422
    assert "notion_token" in response.json()["error"]["message"]


async def test_idempotency_key_returns_same_job(client, broker):
    headers = {"Idempotency-Key": "click-1"}
    first = await client.post("/jobs", json={"youtube_url": URL, "notion_token": "t"}, headers=headers)
    second = await client.post("/jobs", json={"youtube_url": URL, "notion_token": "t"}, headers=headers)
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(broker.published) == 1


async def test_broker_outage_returns_503(store):
    settings = make_settings(token_encryption_key=KEY)
    app = create_app(settings, store=store, broker=RecordingBroker(fail=True), http=httpx.AsyncClient())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        response = await _submit(c)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BROKER_UNAVAILABLE"


async def test_status_poll_is_read_only(client, store):
    job_id = (await _submit(client)).json()["job_id"]
    before = await store.get(job_id)
    for _ in range(3):
        response = await client.get(f"/jobs/{job_id}")
        assert response.status_code == 200
    assert await store.get(job_id) == before
    body = response.json()
    assert {"job_id", "status", "progress", "video_title", "duration_sec", "warnings", "error", "result"} <= set(body)
    assert body["result"] is None and body["warnings"] == []


async def test_status_history(client):
    job_id = (await _submit(client)).json()["job_id"]
    history = (await client.get(f"/jobs/{job_id}?include=history")).json()["history"]
    assert history[0]["status"] == "QUEUED"


async def test_unknown_job_is_404(client):
    for path in ("/jobs/does-not-exist", "/jobs/does-not-exist/results"):
        response = await client.get(path)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "JOB_NOT_FOUND"


async def test_results_409_until_terminal(client, store):
    job_id = (await _submit(client)).json()["job_id"]
    response = await client.get(f"/jobs/{job_id}/results")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "JOB_NOT_FINISHED"

    for status in (JobStatus.TRANSCRIBING, JobStatus.SUMMARIZING, JobStatus.EXTRACTING_FRAMES, JobStatus.PUBLISHING):
        await store.transition(job_id, status)
    await store.transition(
        job_id,
        JobStatus.COMPLETED_WITH_WARNINGS,
        result=JobResult(notion_page_url="https://www.notion.so/abc", notion_page_id="abc", frames_requested=6, frames_published=5),
        warnings=[JobWarning(code="FRAME_EXTRACTION_FAILED", message="m", timestamp_sec=461)],
    )
    body = (await client.get(f"/jobs/{job_id}/results")).json()
    assert body["status"] == "COMPLETED_WITH_WARNINGS"
    assert body["result"]["notion_page_url"] == "https://www.notion.so/abc"
    assert body["result"]["frames_requested"] == 6 and body["result"]["frames_published"] == 5
    assert body["warnings"][0]["timestamp_sec"] == 461


async def test_healthz(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["checks"] == {"gateway": "ok", "redis": "ok", "broker": "ok"}


async def test_oauth_config_and_exchange(client):
    config = (await client.get("/oauth/notion/config")).json()
    assert config == {"enabled": True, "client_id": "cid", "authorize_url": "https://api.notion.com/v1/oauth/authorize"}
    token = await client.post("/oauth/notion/token", json={"code": "c", "redirect_uri": "https://x.chromiumapp.org/notion"})
    assert token.status_code == 200
    assert token.json()["access_token"] == "ntn_x" and token.json()["workspace_name"] == "WS"
