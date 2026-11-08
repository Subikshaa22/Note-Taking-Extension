"""YouTube link -> summary bullets + timestamps of important visuals (videos up to 20 min).

Usage:
    Put GEMINI_API_KEY=... in a .env file next to this script, then:
    python pipeline.py "https://www.youtube.com/watch?v=VIDEO_ID"

Env vars (all optional except the key):
    GEMINI_API_KEY   Google AI Studio key
    LLM_MODEL        default: gemini-3.8-flash
    LLM_FALLBACK_MODEL  tried after LLM_MODEL keeps failing (default: gemini-2.5-flash)
    LLM_TIMEOUT_SEC     give up on a single LLM call after this long (default: 60)
    LLM_BACKOFF_SEC     retry delays per model (default: 5,15,45)
    MAX_FRAMES       default: 8
"""
import json
import os
import re
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import yt_dlp
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from youtube_transcript_api import YouTubeTranscriptApi

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))  # real env vars still win

MAX_DURATION_SEC = 20 * 60
MODEL = os.getenv("LLM_MODEL", "gemini-3.5-flash")
FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "gemini-3.8-flash")
BACKOFF_SEC = [int(s) for s in os.getenv("LLM_BACKOFF_SEC", "5,15,45").split(",") if s]
LLM_TIMEOUT_SEC = int(os.getenv("LLM_TIMEOUT_SEC", "60"))
MAX_FRAMES = int(os.getenv("MAX_FRAMES", "8"))


class PipelineError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class FrameRequest(BaseModel):
    timestamp_sec: int = Field(description="Second in the video where the visual is on screen")
    caption: str = Field(description="What is shown and why it was flagged as important")


class Notes(BaseModel):
    title: str
    bullets: list[str]
    frames: list[FrameRequest]


# ---------- 1. URL / metadata ----------

def parse_video_id(url: str) -> str:
    u = urlparse(url)
    host = (u.hostname or "").removeprefix("www.").removeprefix("m.")
    vid = None
    if host == "youtu.be":
        vid = u.path.lstrip("/")
    elif host in ("youtube.com", "music.youtube.com"):
        if u.path == "/watch":
            vid = parse_qs(u.query).get("v", [None])[0]
        elif u.path.startswith(("/shorts/", "/embed/", "/live/")):
            vid = u.path.split("/")[2]
    if not vid or not re.fullmatch(r"[\w-]{11}", vid):
        raise PipelineError("INVALID_URL", "Not a valid YouTube video URL.")
    return vid


def get_metadata(video_id: str) -> dict:
    """Title + duration without downloading the video; enforces the 20 min cap."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except yt_dlp.utils.DownloadError as e:
        raise PipelineError("VIDEO_UNAVAILABLE", f"Video is private or unavailable: {e}") from e
    duration = int(info.get("duration") or 0)
    if info.get("is_live"):
        raise PipelineError("LIVE_NOT_SUPPORTED", "Live streams are not supported.")
    if duration > MAX_DURATION_SEC:
        raise PipelineError(
            "VIDEO_TOO_LONG",
            f"Video is {duration // 60}m{duration % 60}s; the limit is 20 minutes.",
        )
    return {"title": info.get("title", ""), "duration_sec": duration}


# ---------- 2. Transcript ----------

def get_transcript(video_id: str) -> list[dict]:
    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=["en", "en-US", "en-GB"])
    except Exception as e:
        raise PipelineError("TRANSCRIPT_UNAVAILABLE", f"Could not fetch transcript: {e}") from e
    return [{"start": int(s.start), "text": s.text.replace("\n", " ").strip()} for s in fetched]


def format_transcript(segments: list[dict]) -> str:
    return "\n".join(f"[{s['start'] // 60:02d}:{s['start'] % 60:02d}] {s['text']}" for s in segments)


# ---------- 3. LLM synthesis ----------

PROMPT = """You are a note-taking assistant. Below is the timestamped transcript of a video
that is {duration} seconds long. Produce study notes.

Return:
- title: a short title for the notes.
- bullets: concise summary bullet points, in the order topics appear in the video.
- frames: up to {max_frames} moments where a visually important diagram, slide, chart,
  table, code or demo is probably on screen.

Rules for frames:

- Pick frames such that when a certain topic is being explained you only pick the first and last frame for that topic.
- Pick the second where the visual is being explained, not just first mentioned.
- timestamp_sec must be an integer between 0 and {duration}, taken from the transcript.
- Space frames at least 10 seconds apart and list them in chronological order.
- caption: 1-2 sentences saying what is shown AND why it matters / why you flagged it.
- If nothing looks visual, return an empty frames list. Never invent visuals.

TRANSCRIPT:
{transcript}
"""
# - Only flag a moment if the speech implies something visual is being shown
#   (e.g. "as you can see", "this diagram", "on this slide", "here's the architecture").

TRANSIENT_MARKERS = ("429", "500", "503", "504", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED")


def _is_transient(e: Exception) -> bool:
    if isinstance(e, (httpx.TimeoutException, TimeoutError)):
        return True
    return any(m in str(e) for m in TRANSIENT_MARKERS)


def _call_llm(client: genai.Client, prompt: str) -> Notes:
    """Retry overloaded/rate-limited calls with backoff, then try the fallback model."""
    models = [MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL and FALLBACK_MODEL != MODEL else [])
    last: Exception | None = None
    for model in models:
        for attempt in range(len(BACKOFF_SEC) + 1):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=Notes,
                        temperature=0.2,
                    ),
                )
                return Notes.model_validate_json(resp.text)
            except Exception as e:
                if not _is_transient(e):
                    raise
                last = e
                if attempt < len(BACKOFF_SEC):
                    wait = BACKOFF_SEC[attempt]
                    print(f"[{model}] transient error, retrying in {wait}s...", file=sys.stderr)
                    time.sleep(wait)
    raise last


def synthesize(transcript: list[dict], duration: int, title_hint: str) -> Notes:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise PipelineError("CONFIG_ERROR", "GEMINI_API_KEY is not set.")
    client = genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=LLM_TIMEOUT_SEC * 1000),  # ms
    )

    # A 20-minute transcript is ~4-6k words, which fits in one call.
    prompt = PROMPT.format(
        duration=duration,
        max_frames=MAX_FRAMES,
        transcript=format_transcript(transcript),
    )
    try:
        notes = _call_llm(client, prompt)
    except Exception as e:
        msg = str(e)
        if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            raise PipelineError("RATE_LIMITED", "LLM rate limit hit (429); retry later.") from e
        raise PipelineError("LLM_FAILED", f"LLM call failed: {e}") from e

    return validate_frames(notes, duration, title_hint)


def validate_frames(notes: Notes, duration: int, title_hint: str) -> Notes:
    """Don't trust the model: clamp, de-dup, space out and cap the timestamps."""
    kept: list[FrameRequest] = []
    for f in sorted(notes.frames, key=lambda f: f.timestamp_sec):
        if not (0 <= f.timestamp_sec <= max(duration - 1, 0)) or not f.caption.strip():
            continue
        if kept and f.timestamp_sec - kept[-1].timestamp_sec < 10:
            continue
        kept.append(f)
    notes.frames = kept[:MAX_FRAMES]
    notes.title = notes.title.strip() or title_hint
    return notes


# ---------- 4. Orchestration ----------

def run(url: str) -> dict:
    video_id = parse_video_id(url)
    meta = get_metadata(video_id)
    transcript = get_transcript(video_id)
    notes = synthesize(transcript, meta["duration_sec"], meta["title"])
    return {
        "video_id": video_id,
        "video_title": meta["title"],
        "duration_sec": meta["duration_sec"],
        "title": notes.title,
        "bullets": notes.bullets,
        "frames": [
            {"timestamp": f"{f.timestamp_sec // 60:02d}:{f.timestamp_sec % 60:02d}", **f.model_dump()}
            for f in notes.frames
        ],
    }


def save_result(text: str) -> str:
    """Write to result.txt, or result1.txt, result2.txt, ... if that name is taken."""
    folder = os.path.dirname(os.path.abspath(__file__))
    n = 0
    while True:
        path = os.path.join(folder, f"result{n or ''}.txt")
        try:
            with open(path, "x", encoding="utf-8") as f:  # "x" never overwrites
                f.write(text + "\n")
            return path
        except FileExistsError:
            n += 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252

    # On Windows, Ctrl+C is not delivered while the main thread sits in a blocking
    # socket read. Run the work in a daemon thread and keep the main thread waking
    # up so the interrupt is handled promptly.
    outcome: dict = {}

    def _worker():
        try:
            outcome["result"] = run(sys.argv[1])
        except BaseException as e:  # reported by the main thread
            outcome["error"] = e

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    try:
        while t.is_alive():
            t.join(timeout=0.2)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        os._exit(130)

    err = outcome.get("error")
    if isinstance(err, PipelineError):
        print(json.dumps({"error": {"code": err.code, "message": err.message}}, indent=2))
        sys.exit(1)
    if err:
        raise err
    output = json.dumps(outcome["result"], indent=2, ensure_ascii=False)
    print(output)
    print(f"Saved to {save_result(output)}", file=sys.stderr)
