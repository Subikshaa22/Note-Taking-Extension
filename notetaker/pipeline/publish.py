"""Stage 4 - upload frames and assemble the Notion page, exactly once per job.

Progress is checkpointed in the job's "publish" artifact after every side effect:
  parent       resolved destination
  uploads      timestamp -> Notion file_upload id (+ upload time, uploads expire after 1h)
  page_id/url  set immediately after the page is created
  appended     how many body blocks have been written (bodies > 100 blocks need appends)
A redelivered PUBLISH message resumes from the checkpoint. The one unrecorded window - a
crash after Notion created the page but before its id was saved - is closed by looking for
a page under the same parent whose first block carries this job's id marker.
"""
import logging
import time

from notetaker.domain.errors import ErrorCode, FatalError, RateLimitedError, RetryableError, WarningCode
from notetaker.domain.models import JobResult, JobWarning, Notes, VideoMeta, format_timestamp
from notetaker.domain.status import JobStatus
from notetaker.pipeline.context import StageContext, StageResult
from notetaker.pipeline.notion_page import (
    MAX_BLOCKS_PER_REQUEST,
    PlacedFrame,
    build_page_blocks,
    job_marker,
    page_title_property,
)
from notetaker.services.notion import NotionClient, NotionNotFound

log = logging.getLogger(__name__)

# Notion discards file uploads that are not attached to a block within an hour.
_UPLOAD_REUSE_SEC = 50 * 60
_FATAL_FOR_JOB = {ErrorCode.NOTION_UNAUTHORIZED, ErrorCode.NOTION_FORBIDDEN}


async def resolve_parent(notion: NotionClient, requested_id: str | None, default_id: str | None) -> dict:
    """Return {"type": "page_id"|"database_id", "id": ..., "title_property": ...}.

    Order: the job's notion_parent_id, then NOTION_DEFAULT_PARENT_ID, then the most recently
    edited top-level page the integration can access, then the shared root above the most
    recently edited page (see _shared_root).
    """
    candidate = requested_id or default_id
    if candidate:
        try:
            page = await notion.retrieve_page(candidate)
            return {"type": "page_id", "id": page["id"], "title_property": "title"}
        except (NotionNotFound, FatalError) as exc:
            if isinstance(exc, FatalError) and exc.code in _FATAL_FOR_JOB:
                raise
        try:
            database = await notion.retrieve_database(candidate)
        except (NotionNotFound, FatalError) as exc:
            if isinstance(exc, FatalError) and exc.code in _FATAL_FOR_JOB:
                raise
            raise FatalError(
                ErrorCode.NOTION_PARENT_NOT_FOUND,
                "The Notion destination page was not found or is not shared with the integration.",
            ) from exc
        title_prop = next((name for name, p in database.get("properties", {}).items() if p.get("type") == "title"), "Name")
        return {"type": "database_id", "id": database["id"], "title_property": title_prop}

    pages = await notion.search_pages()
    if not pages:
        raise FatalError(
            ErrorCode.NOTION_NO_DESTINATION,
            "No Notion page is shared with the integration. Share a page or choose a destination in the extension.",
        )
    top_level = [p for p in pages if p.get("parent", {}).get("type") == "workspace"]
    if top_level:
        chosen = top_level[0]
    else:
        # Prefer an ordinary page over a database row as the starting point.
        start = next((p for p in pages if p.get("parent", {}).get("type") != "database_id"), pages[0])
        chosen = await _shared_root(notion, start)
    return {"type": "page_id", "id": chosen["id"], "title_property": "title"}


_MAX_PARENT_HOPS = 20


async def _shared_root(notion: NotionClient, page: dict) -> dict:
    """Walk up from `page` to the highest ancestor the integration can still open.

    Sharing a page with an integration also shares everything inside it, including the notes
    pages this app creates there. The most recently edited page is therefore often the last
    notes page; using it as the parent would nest each new note inside the previous one. The
    page that was actually shared is the one whose parent the integration cannot see.
    """
    for _ in range(_MAX_PARENT_HOPS):
        parent = page.get("parent", {})
        if parent.get("type") != "page_id":
            break
        try:
            page = await notion.retrieve_page(parent["page_id"])
        except NotionNotFound:
            break
        except FatalError as exc:
            if exc.code != ErrorCode.NOTION_FORBIDDEN:
                raise
            break
    return page


async def _first_block_text(notion: NotionClient, page_id: str) -> str:
    async for block in notion.iter_children(page_id):
        payload = block.get(block.get("type"), {})
        return "".join(t.get("plain_text", "") for t in payload.get("rich_text", []))
    return ""


async def find_existing_page(notion: NotionClient, parent: dict, title: str, job_id: str) -> dict | None:
    """Find a page this job already created (crash between page creation and checkpoint)."""
    marker = job_marker(job_id)
    if parent["type"] == "database_id":
        candidates = await notion.query_database(
            parent["id"], {"property": parent["title_property"], "title": {"equals": title}}
        )
        candidate_ids = [c["id"] for c in candidates]
    else:
        candidate_ids = [
            b["id"]
            async for b in notion.iter_children(parent["id"])
            if b.get("type") == "child_page" and b["child_page"].get("title") == title
        ]
    for page_id in reversed(candidate_ids):
        if marker in await _first_block_text(notion, page_id):
            return await notion.retrieve_page(page_id)
    return None


async def _upload_frames(
    ctx: StageContext, notion: NotionClient, notes: Notes, state: dict
) -> tuple[list[PlacedFrame], list[JobWarning]]:
    outcomes = await ctx.store.frame_outcomes(ctx.job_id)
    uploads: dict = state.setdefault("uploads", {})
    placed: list[PlacedFrame] = []
    warnings: list[JobWarning] = []
    captured = [f for f in notes.frames if outcomes.get(f.timestamp_sec, {}).get("ok")]

    for idx, frame in enumerate(captured, start=1):
        key = str(frame.timestamp_sec)
        cached = uploads.get(key)
        if cached and (state.get("page_id") or time.time() - cached["at"] < _UPLOAD_REUSE_SEC):
            placed.append(PlacedFrame(frame.timestamp_sec, frame.caption, cached["id"]))
            continue
        await ctx.report(f"Uploading frame {idx} of {len(captured)} to Notion")
        image = await ctx.store.load_frame(ctx.job_id, frame.timestamp_sec)
        try:
            if image is None:
                raise FatalError(ErrorCode.NOTION_FAILED, "captured frame expired from storage")
            upload_id = await notion.upload_image(f"frame-{frame.timestamp_sec:05d}.jpg", image)
        except RateLimitedError:
            raise
        except (RetryableError, FatalError) as exc:
            if exc.code in _FATAL_FOR_JOB or (exc.retryable and not ctx.is_last_attempt):
                raise
            warnings.append(
                JobWarning(
                    code=WarningCode.FRAME_UPLOAD_FAILED,
                    message=f"Frame at {format_timestamp(frame.timestamp_sec)} could not be uploaded to Notion and was left out.",
                    timestamp_sec=frame.timestamp_sec,
                )
            )
            continue
        uploads[key] = {"id": upload_id, "at": time.time()}
        await ctx.store.save_artifact(ctx.job_id, "publish", state)
        placed.append(PlacedFrame(frame.timestamp_sec, frame.caption, upload_id))
    return placed, warnings


async def run(ctx: StageContext) -> StageResult:
    store, job_id = ctx.store, ctx.job_id
    notes = await store.load_artifact(job_id, "notes", Notes)
    meta = await store.load_artifact(job_id, "meta", VideoMeta)
    if notes is None or meta is None:
        raise RuntimeError("publish stage ran before notes were stored")

    state = await store.load_artifact_dict(job_id, "publish") or {}
    notion = ctx.services.notion(ctx.decrypt_token())

    if "parent" not in state:
        await ctx.report("Finding the Notion destination")
        state["parent"] = await resolve_parent(notion, ctx.job.notion_parent_id, ctx.settings.notion_default_parent_id or None)
        await store.save_artifact(job_id, "publish", state)
    parent = state["parent"]

    placed, upload_warnings = await _upload_frames(ctx, notion, notes, state)
    blocks = build_page_blocks(notes, meta, placed, job_id)

    if not state.get("page_id") and state.get("create_started"):
        await ctx.report("Checking for a page from an interrupted attempt")
        existing = await find_existing_page(notion, parent, notes.title, job_id)
        if existing:
            log.info("job %s: adopting page %s created by an earlier attempt", job_id, existing["id"])
            state.update(page_id=existing["id"], page_url=existing["url"], appended=min(len(blocks), MAX_BLOCKS_PER_REQUEST))
            await store.save_artifact(job_id, "publish", state)

    if not state.get("page_id"):
        await ctx.report("Creating the Notion page")
        await ctx.lease.ensure_held()
        state["create_started"] = True
        await store.save_artifact(job_id, "publish", state)
        first = blocks[:MAX_BLOCKS_PER_REQUEST]
        page = await notion.create_page(
            {parent["type"]: parent["id"]},
            {parent["title_property"]: page_title_property(notes.title)["title"]},
            first,
            icon={"type": "emoji", "emoji": "📝"},
        )
        state.update(page_id=page["id"], page_url=page["url"], appended=len(first))
        await store.save_artifact(job_id, "publish", state)

    while state["appended"] < len(blocks):
        await ctx.lease.ensure_held()
        batch = blocks[state["appended"] : state["appended"] + MAX_BLOCKS_PER_REQUEST]
        await notion.append_children(state["page_id"], batch)
        state["appended"] += len(batch)
        await store.save_artifact(job_id, "publish", state)

    warnings = [*ctx.job.warnings, *upload_warnings]
    result = JobResult(
        notion_page_url=state["page_url"],
        notion_page_id=state["page_id"],
        frames_requested=len(notes.frames),
        frames_published=len(placed),
    )
    status = JobStatus.COMPLETED_WITH_WARNINGS if warnings else JobStatus.COMPLETED
    return StageResult(fields={"result": result, "warnings": warnings}, final_status=status)
