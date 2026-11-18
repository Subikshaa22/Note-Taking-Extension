"""LLM access. `LLMClient` is the interface the pipeline depends on; `GeminiClient` implements it.

Every request first reserves capacity on the shared per-model RPM/TPM windows, so a burst of
jobs across all workers drains at the configured rate instead of triggering provider 429s.
The SDK's own retries are disabled: transient failures are surfaced as RetryableError and
retried through the broker's backoff queues, where the user can see the job waiting.
"""
import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

from notetaker.config import Settings
from notetaker.domain.errors import ErrorCode, FatalError, RateLimitedError, RetryableError
from notetaker.infra.rate_limiter import ServiceLimits, WaitCallback
from notetaker.services.faults import should_inject

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_QUOTA_ID = re.compile(r"quotaId['\"]?\s*:\s*['\"]([^'\"]+)")
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s")

# Receives a short, human-readable line for the popup (e.g. "Gemini is overloaded right now...").
StatusCallback = Callable[[str], Awaitable[None]]


def describe_llm_failure(exc: Exception) -> str:
    """Plain-language reason for a transient LLM failure, for the user (never a raw payload)."""
    text = str(exc)
    if "504" in text or "Timeout" in text or "Deadline" in text:
        return "Gemini took too long to answer"
    if "503" in text or "high demand" in text or "overloaded" in text.lower():
        return "Gemini is overloaded right now"
    if getattr(exc, "code", None) == ErrorCode.LLM_BAD_OUTPUT:
        return "Gemini returned an unusable answer"
    if "500" in text or "502" in text:
        return "Gemini had a server error"
    return "Gemini had a temporary problem"


def estimate_tokens(text: str) -> int:
    """Cheap upper-bound token estimate (~3.5 characters per token for English)."""
    return math.ceil(len(text) / 3.5)


# Per-image cost for the shared TPM limiter. Gemini bills images by the requested media
# resolution (measured on a 640x360 JPEG: low ~266, medium ~529, high/default ~1100 tokens).
IMAGE_TOKENS = {"low": 280, "medium": 560, "high": 1120}
_MEDIA_RESOLUTION = {
    "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
    "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
    "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
}

# A multimodal prompt: text pieces and JPEG images, in the order the model should read them.
PromptPart = str | bytes


class LLMClient(Protocol):
    async def generate(
        self, prompt: str, schema: type[T], on_wait: WaitCallback | None = None, *, on_status: StatusCallback | None = None
    ) -> T: ...

    async def generate_from_parts(
        self,
        parts: list[PromptPart],
        schema: type[T],
        on_wait: WaitCallback | None = None,
        timeout_sec: float | None = None,
        image_resolution: str = "high",
        *,
        on_status: StatusCallback | None = None,
    ) -> T: ...


class GeminiClient:
    def __init__(self, settings: Settings, limits: ServiceLimits):
        if not settings.gemini_api_key:
            raise FatalError(ErrorCode.CONFIG_ERROR, "GEMINI_API_KEY is not set.")
        self._settings = settings
        self._limits = limits
        self._client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(
                timeout=settings.llm_timeout_sec * 1000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        self._models = [settings.llm_model]
        if settings.llm_fallback_model and settings.llm_fallback_model != settings.llm_model:
            self._models.append(settings.llm_fallback_model)

    async def generate_from_parts(
        self,
        parts: list[PromptPart],
        schema: type[T],
        on_wait: WaitCallback | None = None,
        timeout_sec: float | None = None,
        image_resolution: str = "high",
        *,
        on_status: StatusCallback | None = None,
    ) -> T:
        """Text interleaved with JPEG images (e.g. a label before every video frame).

        Building and encoding a request with hundreds of images takes several seconds of CPU,
        so it runs on a thread (`blocking=True`); on the event loop it would starve the job
        lease's keep-alive and the RabbitMQ heartbeat.
        """
        contents = await asyncio.to_thread(
            lambda: [
                types.Part.from_bytes(data=p, mime_type="image/jpeg") if isinstance(p, bytes) else types.Part.from_text(text=p)
                for p in parts
            ]
        )
        per_image = IMAGE_TOKENS[image_resolution]
        tokens = sum(per_image if isinstance(p, bytes) else estimate_tokens(p) for p in parts)
        extra = {"media_resolution": _MEDIA_RESOLUTION[image_resolution]}
        if timeout_sec:
            extra["http_options"] = types.HttpOptions(timeout=int(timeout_sec * 1000), retry_options=types.HttpRetryOptions(attempts=1))
        return await self._generate(contents, tokens, schema, on_wait, extra, blocking=True, on_status=on_status)

    async def generate(
        self, prompt: str, schema: type[T], on_wait: WaitCallback | None = None, *, on_status: StatusCallback | None = None
    ) -> T:
        return await self._generate(prompt, estimate_tokens(prompt), schema, on_wait, {}, on_status=on_status)

    async def _generate(
        self,
        contents,
        tokens: int,
        schema: type[T],
        on_wait: WaitCallback | None,
        extra: dict,
        blocking: bool = False,
        on_status: StatusCallback | None = None,
    ) -> T:
        """Try the primary model, then the fallback model on a transient failure.

        Switching models is reported through `on_status` so the user sees why it is slow.
        """
        last_error: RetryableError | None = None
        quota_error: FatalError | None = None
        for index, model in enumerate(self._models):
            try:
                return await self._generate_with(model, contents, tokens, schema, on_wait, extra, blocking)
            except RetryableError as exc:
                log.warning("model %s failed transiently: %s", model, exc)
                last_error = exc
                if on_status and index + 1 < len(self._models) and not isinstance(exc, RateLimitedError):
                    await on_status(f"{describe_llm_failure(exc)}. Trying the backup model")
            except FatalError as exc:
                if exc.code == ErrorCode.LLM_QUOTA_EXHAUSTED:
                    # Another model has its own daily quota, so it is still worth trying.
                    log.warning("model %s: %s", model, exc.message)
                    quota_error = exc
                    continue
                # A misconfigured fallback must not hide the primary model's transient error.
                if last_error is not None:
                    raise last_error
                raise
        # A transient failure may clear up; an exhausted daily quota will not until it resets.
        if last_error is not None:
            raise last_error
        assert quota_error is not None
        raise quota_error

    async def _generate_with(
        self, model: str, contents, tokens: int, schema: type[T], on_wait: WaitCallback | None, extra: dict, blocking: bool
    ) -> T:
        await self._limits.llm(model, tokens, on_wait)
        if should_inject(self._settings.fault_llm_429_rate, f"429 from {model}"):
            raise RateLimitedError(f"llm:{model}", f"{model} rate limit reached (injected).", 5)
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=self._settings.llm_temperature,
            **extra,
        )
        try:
            if blocking:
                response = await asyncio.to_thread(
                    self._client.models.generate_content, model=model, contents=contents, config=config
                )
            else:
                response = await self._client.aio.models.generate_content(model=model, contents=contents, config=config)
        except genai_errors.APIError as exc:
            raise await self._classify(exc, model) from exc
        except Exception as exc:  # timeouts, connection resets, DNS hiccups
            raise RetryableError(ErrorCode.LLM_FAILED, f"LLM request failed: {type(exc).__name__}") from exc

        text = response.text
        if not text:
            reason = response.candidates[0].finish_reason if response.candidates else "no candidates"
            raise RetryableError(ErrorCode.LLM_BAD_OUTPUT, f"LLM returned no content ({reason}).")
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            raise RetryableError(ErrorCode.LLM_BAD_OUTPUT, "LLM returned malformed JSON.") from exc

    async def _classify(self, exc: genai_errors.APIError, model: str) -> Exception:
        code = exc.code or 0
        message = exc.message or str(exc)
        if code == 429:
            match = _RETRY_DELAY.search(str(exc.details))
            retry_after = float(match.group(1)) if match else None
            quotas = sorted(set(_QUOTA_ID.findall(str(exc.details))))
            log.warning("%s returned 429 (quota %s, retry after %ss)", model, ", ".join(quotas) or "unknown", retry_after)
            if any("PerDay" in q for q in quotas):
                hours = f" It resets in about {math.ceil(retry_after / 3600)} hours." if retry_after else ""
                return FatalError(
                    ErrorCode.LLM_QUOTA_EXHAUSTED,
                    f"The AI service's daily request quota for {model} is used up.{hours}",
                )
            await self._limits.cooldown(f"llm:{model}", retry_after or 10)
            return RateLimitedError(f"llm:{model}", f"{model} rate limit reached.", retry_after)
        if code in (401, 403) or (code == 400 and "api key" in message.lower()):
            return FatalError(ErrorCode.LLM_AUTH_FAILED, "The LLM API key was rejected.")
        if code == 404:
            return FatalError(ErrorCode.LLM_REQUEST_REJECTED, f"LLM model '{model}' was not found.")
        if code in (400, 413, 422):
            return FatalError(ErrorCode.LLM_REQUEST_REJECTED, f"LLM rejected the request: {message[:300]}")
        return RetryableError(ErrorCode.LLM_FAILED, f"LLM service error {code}: {message[:300]}")
