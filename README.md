# Video Notes: YouTube → Notion note-taking agent

Click **Take Notes** on a YouTube video. A distributed backend turns it into structured study notes (summary,
key takeaways, sectioned bullets) with **real frames captured from the video**, each labelled with its timestamp
and an AI-written caption, and publishes them as a Notion page. For whiteboard and slide videos, the frames are
chosen by scanning the video so that every board state appears once, at its most complete and with the presenter
blocking as little as possible.

```mermaid
flowchart LR
  subgraph Chrome
    CS[Content script<br/>Take Notes button] --> BG[Background<br/>service worker]
    BG <--> PU[Popup]
  end
  BG -- "POST /jobs, GET /jobs/:id" --> GW[API gateway<br/>FastAPI]
  GW -- enqueue --> MQ[(RabbitMQ<br/>notes.jobs / retry / DLQ)]
  GW <-- job state --> RD[(Redis<br/>state, leases, rate limits)]
  MQ --> W1[Worker 1] & W2[Worker 2] & WN[Worker N]
  W1 & W2 & WN <--> RD
  W1 & W2 & WN --> YT[YouTube] & LLM[Gemini] & NO[Notion]
```

Design, requirement traceability and the roadmap are in **[docs/PROJECT.md](docs/PROJECT.md)**.

---

## Quick start

**You need:** Docker Desktop (Compose v2), Google Chrome, a [Gemini API key](https://aistudio.google.com/apikey),
and a Notion account.

```bash
cp .env.example .env          # then set GEMINI_API_KEY (and the Notion OAuth values, see below)
docker compose up --build -d  # RabbitMQ, Redis, gateway and 2 workers
curl http://localhost:8000/healthz
```

A healthy stack answers `200` with `{"status": "ok", "checks": {"gateway": "ok", "redis": "ok", "broker": "ok"}, ...}`.

**Load the extension** (no build step): open `chrome://extensions`, enable **Developer mode**, click
**Load unpacked** and pick the `extension/` folder. Pin **Video Notes** to the toolbar.

**Connect Notion** from the extension popup (see [Notion setup](#notion-setup)), open any YouTube video
of 20 minutes or less, and press **Take Notes** in the player toolbar. The popup shows progress and a
**View in Notion** button when the page is ready.

> Port 8000 already in use? Set `GATEWAY_PORT=8001` in `.env` and change the gateway address in the
> extension's Settings page to `http://localhost:8001`.

## Base URL and API

Base URL: **`http://localhost:8000`** (configurable with `GATEWAY_PORT`).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/jobs` | Submit `{"youtube_url", "notion_token", "notion_parent_id"?}` → `202` + `Location: /jobs/{id}`. Optional `Idempotency-Key` header. |
| `GET` | `/jobs/{id}` | Read-only status: `status`, `progress`, `video_title`, `duration_sec`, `warnings`, `error`, `result`, plus `detail`, `retry`, `worker_id`, `attempt`. `?include=history` adds the audit trail. `404` if unknown. |
| `GET` | `/jobs/{id}/results` | Final outcome (`notion_page_url`, `frames_requested`, `frames_published`, warnings). `409` until the job is finished. |
| `GET` | `/healthz` | Gateway, Redis and RabbitMQ reachability plus queue depths. `503` if anything is down. |
| `GET` | `/oauth/notion/config` | Public OAuth settings for the extension. |
| `POST` | `/oauth/notion/token` | Exchanges an OAuth code for a token. The client secret stays on the server and the token is not stored. |

Every error response looks like `{"error": {"code": "...", "message": "..."}}`. Interactive docs are at
`http://localhost:8000/docs`.

```bash
curl -i -X POST http://localhost:8000/jobs -H 'Content-Type: application/json' \
  -d '{"youtube_url":"https://www.youtube.com/watch?v=dQw4w9WgXcQ","notion_token":"ntn_..."}'
curl http://localhost:8000/jobs/<job_id>
```

## Notion setup

Pick **one** of the two options.

**A. One-time OAuth sign-in (public integration).** This is what the extension's **Connect Notion** button uses.
1. At <https://www.notion.so/profile/integrations>, create a **public** integration with the
   *Read content*, *Update content* and *Insert content* capabilities.
2. Add this redirect URI exactly:
   `https://ebjbofalkfocmejjoggjeegajhihboae.chromiumapp.org/notion`
   (the extension id is pinned by the `key` in `extension/manifest.json`, so it's the same on every machine).
3. Put the OAuth client id and secret in `.env` as `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET`, then run
   `docker compose up -d`.
4. In the popup, click **Connect Notion** and choose the pages to share.

**B. Internal integration token.** No OAuth app is needed.
1. Create an **internal** integration and copy its secret (`ntn_...`).
2. Open the Notion page you want notes in, then use **⋯ → Connections** to add the integration.
3. Paste the secret under **Notion access** on the extension's Settings page.

### Default Notion destination

If a job has no `notion_parent_id`, which is the case when the extension's Settings page leaves the
destination empty, the page is created under:
1. `NOTION_DEFAULT_PARENT_ID`, if it's set, otherwise
2. the **most recently edited top-level page shared with the integration**.

If nothing is shared with the integration, the job fails with `NOTION_NO_DESTINATION`. The parent can be a page
or a database (for a database, the note becomes a new row).

## Configuration

All settings are environment variables, read from `.env` by Docker Compose. `.env.example` lists every one with
its default.

| Variable | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` | **required** | LLM API key (Google AI Studio). |
| `LLM_MODEL` | `gemini-3.5-flash` | Primary model. |
| `LLM_FALLBACK_MODEL` | *(empty)* | Tried when the primary model is rate-limited or overloaded (`.env.example` sets `gemini-3.8-flash`). |
| `LLM_RPM` / `LLM_TPM` | `10` / `250000` | **Global LLM rate limit** (requests and estimated tokens per minute, per model) shared by all workers. |
| `NOTION_RPS` | `3` | Global Notion request rate. |
| `YOUTUBE_RPM` | `60` | Global YouTube metadata/transcript request rate. |
| `NOTION_CLIENT_ID` / `NOTION_CLIENT_SECRET` | *(empty)* | Notion OAuth client credentials (option A above). |
| `NOTION_DEFAULT_PARENT_ID` | *(empty)* | Default destination page or database. |
| `AMQP_URL` | `amqp://notes:notes@rabbitmq:5672/` | **Broker connection string.** |
| `REDIS_URL` | `redis://redis:6379/0` | State store connection string. |
| `MAX_ATTEMPTS` | `5` | Attempts per stage before a job is dead-lettered. |
| `RETRY_DELAYS_SEC` | `10,30,90` | **Backoff schedule** between attempts; the last delay repeats. |
| `LEASE_TTL_SEC` | `10` | How quickly a killed worker's job can be taken over. |
| `WORKER_CONCURRENCY` | `1` | Stage messages each worker processes at once. |
| `TOKEN_ENCRYPTION_KEY` | dev key in `.env.example` | Fernet key that encrypts Notion tokens in queue messages. Gateway and workers must share it. |
| `MAX_VIDEO_DURATION_SEC` | `1200` | Constraint A: longer videos are rejected. |
| `MAX_FRAMES` / `MIN_FRAME_GAP_SEC` | `15` / `10` | Frame budget per video, and minimum spacing between transcript-chosen frames. |
| `FRAME_SELECTION` | `visual` | `visual`: scan the video and let the vision model pick, for each board/slide state, the moment where it is most complete and least blocked by the presenter. `transcript`: pick moments from the transcript only (faster, no vision call). Either way the published images are real, unedited frames. |
| `FRAME_SCAN_INTERVAL_SEC` / `FRAME_SCAN_MAX_IMAGES` | `5` / `240` | Sampling interval of the visual scan; the interval grows if the video would need more samples than the cap. |
| `FRAME_SCAN_HEIGHT` / `FRAME_SCAN_TIMEOUT_SEC` | `360` / `600` | Resolution of the scan samples (only the model sees them) and time limit for the scan. |
| `LLM_CHUNK_CHARS` | `60000` | Transcripts longer than this are summarised in chunks and merged. |
| `NOTES_LANGUAGE` | `English` | Language the notes are written in. |
| `TRANSCRIPT_LANGUAGES` | `en,en-US,en-GB` | Preferred caption languages (any other language is used as a fallback). |
| `FRAME_MAX_HEIGHT` / `FRAME_TIMEOUT_SEC` / `FRAME_CONCURRENCY` | `720` / `45` / `3` | Frame capture tuning. |
| `YTDLP_COOKIES_FILE` | *(empty)* | Cookies file for YouTube if it asks the server to sign in. |
| `JOB_TTL_SEC` | `604800` | How long job state is kept (7 days). |
| `GATEWAY_PORT` | `8000` | Host port for the gateway. |
| `FAULT_*_RATE` | `0` | Opt-in fault injection for demos (see below). |

Advanced settings you rarely need to change: `LLM_TIMEOUT_SEC` (120), `LLM_TEMPERATURE` (0.2),
`NOTION_VERSION` (`2022-06-28`), `NOTION_API_BASE`, `NOTION_TIMEOUT_SEC` (30), `QUEUE_DELIVERY_LIMIT` (20, a
RabbitMQ-side guard against poison messages), `IDEMPOTENCY_TTL_SEC` (86400), `CORS_ALLOW_ORIGINS` (`*`) and
`LOG_LEVEL` (`INFO`).

## Demonstrating failure handling

| Scenario | How to trigger it | What you'll see |
|---|---|---|
| **Rate limit (429)** | Set `FAULT_LLM_429_RATE=0.5` (or `FAULT_NOTION_429_RATE` / `FAULT_YOUTUBE_429_RATE`) in `.env`, run `docker compose up -d`, then submit a job. To show the shared limiter instead, set `LLM_RPM=2` and click Take Notes on 5 videos in a row. | The popup shows *"Waiting — retrying in 30s…"*. `GET /jobs/{id}` has a `retry` block. With the limiter, jobs show *"Waiting for LLM capacity"* and never reach the provider faster than the limit. |
| **Worker crash** | While a job runs, read its `worker_id` from `GET /jobs/{id}`; the part before the `-` is the container id. Run `docker kill <container-id>` (SIGKILL). | RabbitMQ redelivers the unacked stage to another worker, which takes over within `LEASE_TTL_SEC` and resumes from the last finished stage (`attempt` increments, `worker_id` changes). There's one Notion page in the end. `docker compose up -d` restarts the killed worker. |
| **Graceful degradation** | `FAULT_FRAME_FAILURE_RATE=0.4` (some frames fail) or `=1` (all fail). | `COMPLETED_WITH_WARNINGS`, a page with the frames that succeeded (or text only), and a warning per missing frame. |
| **Dead-letter queue** | Submit a private or removed video, e.g. `https://youtu.be/xxxxxxxxxxx`. For a fast demo, set `MAX_ATTEMPTS=3` and `RETRY_DELAYS_SEC=3,6`. | Retries with backoff, then `DEAD_LETTERED`. The message is in `notes.dlq`, which you can inspect in the RabbitMQ UI at <http://localhost:15672> (login `notes` / `notes`). |
| **Constraint A** | Submit a video longer than 20 minutes. | `FAILED` with `VIDEO_TOO_LONG` before any transcript, LLM or frame work happens. |

Scale the pool with `docker compose up -d --scale worker=4`.

## Development

```bash
py -3.12 -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Windows
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt     # macOS/Linux
.venv/Scripts/python -m pytest        # 108 tests; no Docker or network needed
```

To run the services outside Docker, start RabbitMQ and Redis, point `AMQP_URL` / `REDIS_URL` at them, and run
`uvicorn notetaker.gateway.app:create_app --factory --port 8000` and `python -m notetaker.worker.main`. The
worker needs `ffmpeg` and `deno` on `PATH`. To regenerate the extension icons, run `python scripts/generate_icons.py`.

```
notetaker/
  domain/      job states and transitions, models, error codes, URL/id parsing (no I/O)
  infra/       Redis job store, job lease, global rate limiter, RabbitMQ topology, token encryption
  services/    YouTube (yt-dlp), transcripts, ffmpeg frame capture, Gemini, Notion, fault injection
  pipeline/    the four idempotent stages + synthesis (prompts, chunking) + Notion page layout
  worker/      stage processor (ack/retry/DLQ decisions) and worker entrypoint
  gateway/     FastAPI app, routes, schemas, error shape
extension/     Chrome MV3 extension (content script, background worker, popup, settings)
tests/         unit and integration tests with fakes for Redis, YouTube, LLM and Notion
docs/          design document
```
