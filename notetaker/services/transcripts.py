"""Timestamped transcripts via youtube-transcript-api (captions, manual or auto-generated)."""
import asyncio

import youtube_transcript_api as yta

from notetaker.config import Settings
from notetaker.domain.errors import ErrorCode, FatalError, RateLimitedError, RetryableError
from notetaker.domain.models import Transcript, TranscriptSegment
from notetaker.infra.rate_limiter import ServiceLimits


class TranscriptClient:
    def __init__(self, settings: Settings, limits: ServiceLimits):
        self._languages = settings.transcript_languages
        self._limits = limits
        self._api = yta.YouTubeTranscriptApi()

    def _fetch_sync(self, video_id: str) -> Transcript:
        listing = self._api.list(video_id)
        try:
            # Prefers manually created captions over auto-generated ones in these languages.
            chosen = listing.find_transcript(self._languages)
        except yta.NoTranscriptFound:
            # Fall back to any available language; the LLM writes the notes in NOTES_LANGUAGE.
            chosen = next(iter(listing), None)
            if chosen is None:
                raise
        fetched = chosen.fetch()
        segments = [
            TranscriptSegment(start=s.start, duration=s.duration, text=" ".join(s.text.split()))
            for s in fetched.snippets
            if s.text.strip()
        ]
        return Transcript(language_code=fetched.language_code, is_generated=fetched.is_generated, segments=segments)

    async def fetch(self, video_id: str) -> Transcript:
        await self._limits.youtube()
        try:
            transcript = await asyncio.to_thread(self._fetch_sync, video_id)
        except (yta.TranscriptsDisabled, yta.NoTranscriptFound) as exc:
            raise FatalError(
                ErrorCode.TRANSCRIPT_UNAVAILABLE, "This video has no captions, so no transcript is available."
            ) from exc
        except (yta.IpBlocked, yta.RequestBlocked) as exc:
            raise RateLimitedError("youtube", "YouTube is blocking transcript requests from this server.") from exc
        except (yta.VideoUnavailable, yta.VideoUnplayable, yta.AgeRestricted) as exc:
            raise RetryableError(ErrorCode.VIDEO_UNAVAILABLE, "The video is unavailable or restricted.") from exc
        except yta.YouTubeRequestFailed as exc:
            if "429" in str(exc):
                raise RateLimitedError("youtube", "YouTube rate-limited the transcript request.") from exc
            raise RetryableError(ErrorCode.YOUTUBE_FAILED, "YouTube transcript request failed.") from exc
        except yta.CouldNotRetrieveTranscript as exc:
            raise RetryableError(ErrorCode.YOUTUBE_FAILED, "Could not retrieve the transcript.") from exc
        if not transcript.segments:
            raise FatalError(ErrorCode.TRANSCRIPT_UNAVAILABLE, "The video's transcript is empty.")
        return transcript
