"""LLM access. `LLMClient` is the interface the pipeline depends on; `GeminiClient` implements it.

Every request first reserves capacity on the shared per-model RPM/TPM windows, so a burst of
jobs across all workers drains at the configured rate instead of triggering provider 429s.
The SDK's own retries are disabled: transient failures are surfaced as RetryableError and
retried through the broker's backoff queues, where the user can see the job waiting.
"""
import logging
import math
import re
from typing import Protocol, TypeVar

from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import BaseModel, ValidationError

from notetaker.config import Settings
from notetaker.domain.errors import ErrorCode, FatalError, RateLimitedError, RetryableError
from notetaker.infra.rate_limiter import ServiceLimits, WaitCallback

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s")


def estimate_tokens(text: str) -> int:
    """Cheap upper-bound token estimate (~3.5 characters per token for English)."""
    return math.ceil(len(text) / 3.5)


class LLMClient(Protocol):
    async def generate(self, prompt: str, schema: type[T], on_wait: WaitCallback | None = None) -> T: ...


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

    async def generate(self, prompt: str, schema: type[T], on_wait: WaitCallback | None = None) -> T:
        """Try the primary model, then the fallback model on a transient failure."""
        last_error: RetryableError | None = None
        for model in self._models:
            try:
                return await self._generate_with(model, prompt, schema, on_wait)
            except RetryableError as exc:
                log.warning("model %s failed transiently: %s", model, exc)
                last_error = exc
            except FatalError:
                # A misconfigured fallback must not hide the primary model's transient error.
                if last_error is not None:
                    raise last_error
                raise
        assert last_error is not None
        raise last_error

    async def _generate_with(self, model: str, prompt: str, schema: type[T], on_wait: WaitCallback | None) -> T:
        await self._limits.llm(model, estimate_tokens(prompt), on_wait)
        try:
            response = await self._client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=self._settings.llm_temperature,
                ),
            )
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
            await self._limits.cooldown(f"llm:{model}", retry_after or 10)
            return RateLimitedError(f"llm:{model}", f"{model} rate limit reached.", retry_after)
        if code in (401, 403) or (code == 400 and "api key" in message.lower()):
            return FatalError(ErrorCode.LLM_AUTH_FAILED, "The LLM API key was rejected.")
        if code == 404:
            return FatalError(ErrorCode.LLM_REQUEST_REJECTED, f"LLM model '{model}' was not found.")
        if code in (400, 413, 422):
            return FatalError(ErrorCode.LLM_REQUEST_REJECTED, f"LLM rejected the request: {message[:300]}")
        return RetryableError(ErrorCode.LLM_FAILED, f"LLM service error {code}: {message[:300]}")
