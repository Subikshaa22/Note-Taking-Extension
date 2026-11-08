"""Minimal async Notion REST client covering what the publisher needs.

All requests pass through the shared Notion rate limit (default 3 req/s, Notion's documented
average). HTTP failures are mapped onto the domain error hierarchy: 429 and 5xx are
retryable, authentication/permission problems are fatal.
"""
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from notetaker.config import Settings
from notetaker.domain.errors import ErrorCode, FatalError, RateLimitedError, RetryableError
from notetaker.infra.rate_limiter import ServiceLimits

log = logging.getLogger(__name__)


class NotionNotFound(Exception):
    """404 from Notion; callers decide whether that is fatal."""


class NotionClient:
    def __init__(self, token: str, settings: Settings, limits: ServiceLimits, http: httpx.AsyncClient):
        self._limits = limits
        self._http = http
        self._base = settings.notion_api_base.rstrip("/")
        self._auth_headers = {"Authorization": f"Bearer {token}", "Notion-Version": settings.notion_version}

    async def _request(self, method: str, path: str, *, json: Any = None, files: Any = None) -> dict:
        await self._limits.notion()
        headers = dict(self._auth_headers)
        if json is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = await self._http.request(method, f"{self._base}{path}", headers=headers, json=json, files=files)
        except httpx.HTTPError as exc:
            raise RetryableError(ErrorCode.NOTION_FAILED, f"Notion request failed: {type(exc).__name__}") from exc

        if response.is_success:
            return response.json()
        await self._raise_for(response)
        raise AssertionError("unreachable")

    async def _raise_for(self, response: httpx.Response) -> None:
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = body.get("message") or response.text[:300]
        if status == 429:
            retry_after = float(response.headers.get("Retry-After", "1") or 1)
            await self._limits.cooldown("notion", retry_after)
            raise RateLimitedError("notion", "Notion rate limit reached.", retry_after)
        if status == 401:
            raise FatalError(
                ErrorCode.NOTION_UNAUTHORIZED, "Notion rejected the access token. Reconnect Notion in the extension."
            )
        if status == 403:
            raise FatalError(ErrorCode.NOTION_FORBIDDEN, f"The integration lacks permission: {message}")
        if status == 404:
            raise NotionNotFound(message)
        if status == 409 or status >= 500:
            raise RetryableError(ErrorCode.NOTION_FAILED, f"Notion error {status}: {message}")
        raise FatalError(ErrorCode.NOTION_REQUEST_REJECTED, f"Notion rejected the request ({status}): {message}")

    # ---- Pages, databases, blocks ----------------------------------------------------

    async def retrieve_page(self, page_id: str) -> dict:
        return await self._request("GET", f"/pages/{page_id}")

    async def retrieve_database(self, database_id: str) -> dict:
        return await self._request("GET", f"/databases/{database_id}")

    async def search_pages(self, page_size: int = 50) -> list[dict]:
        body = {
            "filter": {"property": "object", "value": "page"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": page_size,
        }
        return (await self._request("POST", "/search", json=body)).get("results", [])

    async def create_page(self, parent: dict, properties: dict, children: list[dict], icon: dict | None = None) -> dict:
        body: dict = {"parent": parent, "properties": properties, "children": children}
        if icon:
            body["icon"] = icon
        return await self._request("POST", "/pages", json=body)

    async def append_children(self, block_id: str, children: list[dict]) -> dict:
        return await self._request("PATCH", f"/blocks/{block_id}/children", json={"children": children})

    async def iter_children(self, block_id: str) -> AsyncIterator[dict]:
        cursor = None
        while True:
            suffix = f"?page_size=100{f'&start_cursor={cursor}' if cursor else ''}"
            page = await self._request("GET", f"/blocks/{block_id}/children{suffix}")
            for block in page.get("results", []):
                yield block
            if not page.get("has_more"):
                return
            cursor = page.get("next_cursor")

    async def query_database(self, database_id: str, filter_: dict) -> list[dict]:
        body = {"filter": filter_, "page_size": 100}
        return (await self._request("POST", f"/databases/{database_id}/query", json=body)).get("results", [])

    # ---- File uploads (direct upload API) --------------------------------------------

    async def upload_image(self, filename: str, data: bytes, content_type: str = "image/jpeg") -> str:
        """Upload bytes to Notion; returns the file_upload id to reference from an image block."""
        created = await self._request(
            "POST", "/file_uploads", json={"mode": "single_part", "filename": filename, "content_type": content_type}
        )
        upload_id = created["id"]
        sent = await self._request(
            "POST", f"/file_uploads/{upload_id}/send", files={"file": (filename, data, content_type)}
        )
        if sent.get("status") != "uploaded":
            raise RetryableError(ErrorCode.NOTION_FAILED, f"Notion upload ended in status {sent.get('status')}.")
        return upload_id
