"""LLM synthesis: transcript -> structured notes plus captioned frame timestamps.

Short transcripts are summarised in one call. Long ones are split on caption boundaries into
chunks ("map"), each producing its own sections and frame suggestions in order, followed by
one small "reduce" call that writes the title, summary and key takeaways from the merged
outline. Section order is preserved because chunks are processed and concatenated in order.

The model's frame suggestions are never trusted blindly: `validate_frames` clamps, sorts,
de-duplicates and caps them before anything is extracted.
"""
import math
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from notetaker.domain.models import (
    FrameSuggestion,
    Notes,
    Section,
    Transcript,
    TranscriptSegment,
    VideoMeta,
    format_timestamp,
)
from notetaker.infra.rate_limiter import WaitCallback
from notetaker.services.llm import LLMClient

Reporter = Callable[[str], Awaitable[None]]


class PartialNotes(BaseModel):
    sections: list[Section] = Field(default_factory=list)
    frames: list[FrameSuggestion] = Field(default_factory=list)


class NotesOverview(BaseModel):
    title: str
    summary: str
    key_takeaways: list[str] = Field(default_factory=list)


_FRAME_RULES = """Rules for frames:
- Only flag a moment when the speech clearly refers to something being shown on screen
  ("as you can see", "this diagram", "on this slide", "here is the code", reading out a
  formula or table, walking through a UI or demo). Never invent visuals: if nothing is
  shown, return an empty list.
- Pick the second where the visual is being explained, not the second it is first mentioned.
- For each topic explained with a visual, pick at most two frames: the first moment it is
  explained and its final/complete state (e.g. a diagram after it has been fully drawn).
- timestamp_sec must be an integer between {first_ts} and {last_ts}, taken from the
  transcript timing. Keep frames at least {min_gap} seconds apart, in chronological order.
- caption: 1-2 sentences in {language} saying what is shown AND why it was flagged / why it
  matters for understanding the video. Every frame needs a caption."""

_FULL_PROMPT = """You are an expert note-taker turning a video transcript into study notes.

Video title: {title}
Video length: {length} ({duration} seconds)
Write everything in {language}, whatever the transcript language is.
Each transcript line starts with its time [mm:ss].

Return:
- title: a concise, descriptive title for the notes.
- summary: 2-4 sentences on what the video covers and its main conclusion.
- key_takeaways: 3-6 of the most important ideas, one sentence each.
- sections: the video's structure in order. For each: a short heading, start_sec (where it
  begins) and 2-6 concise bullets with the substance (definitions, steps, numbers, examples).
  No filler such as "the speaker talks about".
- frames: up to {max_frames} moments where a visually important diagram, slide, chart,
  table, code, equation or demo is very likely on screen.

{frame_rules}

TRANSCRIPT:
{transcript}
"""

_CHUNK_PROMPT = """You are an expert note-taker turning a video transcript into study notes.

Video title: {title}
This is part {part} of {parts} of the transcript, covering {start} to {end} of a
{length} video. Write everything in {language}. Each line starts with its time [mm:ss].

Return, for this part only:
- sections: the structure of this part in order. For each: a short heading, start_sec
  (within this part) and 2-6 concise bullets with the substance. No filler.
- frames: up to {max_frames} moments where a visually important diagram, slide, chart,
  table, code, equation or demo is very likely on screen.

{frame_rules}

TRANSCRIPT PART:
{transcript}
"""

_REDUCE_PROMPT = """Below is the section outline of study notes taken from a video titled
"{title}" ({length}). Write in {language}.

Return:
- title: a concise, descriptive title for the notes.
- summary: 2-4 sentences on what the video covers and its main conclusion.
- key_takeaways: 3-6 of the most important ideas, one sentence each.

OUTLINE:
{outline}
"""


def format_transcript(segments: list[TranscriptSegment]) -> str:
    return "\n".join(f"[{format_timestamp(s.start)}] {s.text}" for s in segments)


def chunk_segments(segments: list[TranscriptSegment], max_chars: int) -> list[list[TranscriptSegment]]:
    """Split on caption boundaries so that each chunk's formatted text stays under max_chars."""
    chunks: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    size = 0
    for seg in segments:
        line_len = len(seg.text) + 10
        if current and size + line_len > max_chars:
            chunks.append(current)
            current, size = [], 0
        current.append(seg)
        size += line_len
    if current:
        chunks.append(current)
    return chunks


def _thin_evenly(items: list, keep: int) -> list:
    if len(items) <= keep:
        return items
    if keep == 1:
        return [items[0]]
    step = (len(items) - 1) / (keep - 1)
    return [items[round(i * step)] for i in range(keep)]


def validate_frames(
    frames: list[FrameSuggestion], duration_sec: int, max_frames: int, min_gap_sec: int
) -> list[FrameSuggestion]:
    """Keep in-range, captioned, well-spaced frames; spread the survivors if over the cap."""
    last_valid = max(duration_sec - 1, 0)
    kept: list[FrameSuggestion] = []
    for frame in sorted(frames, key=lambda f: f.timestamp_sec):
        caption = " ".join(frame.caption.split())
        if not caption or not 0 <= frame.timestamp_sec <= last_valid:
            continue
        if kept and frame.timestamp_sec - kept[-1].timestamp_sec < min_gap_sec:
            continue
        kept.append(FrameSuggestion(timestamp_sec=frame.timestamp_sec, caption=caption))
    return _thin_evenly(kept, max_frames)


def normalize_sections(sections: list[Section], duration_sec: int) -> list[Section]:
    cleaned = []
    for section in sections:
        bullets = [" ".join(b.split()) for b in section.bullets if b.strip()]
        heading = " ".join(section.heading.split())
        if not bullets and not heading:
            continue
        start = min(max(section.start_sec, 0), max(duration_sec - 1, 0))
        cleaned.append(Section(heading=heading or "Notes", start_sec=start, bullets=bullets))
    # Stable sort keeps the model's order for sections that share a start time.
    return sorted(cleaned, key=lambda s: s.start_sec)


async def synthesize(
    llm: LLMClient,
    transcript: Transcript,
    meta: VideoMeta,
    *,
    language: str,
    max_frames: int,
    min_gap_sec: int,
    chunk_chars: int,
    report: Reporter,
    on_wait: WaitCallback | None = None,
) -> Notes:
    duration = meta.duration_sec or transcript.end_sec
    common = {
        "title": meta.title,
        "length": format_timestamp(duration),
        "language": language,
    }
    chunks = chunk_segments(transcript.segments, chunk_chars)

    if len(chunks) == 1:
        await report("Writing notes")
        prompt = _FULL_PROMPT.format(
            **common,
            duration=duration,
            max_frames=max_frames,
            frame_rules=_FRAME_RULES.format(first_ts=0, last_ts=max(duration - 1, 0), min_gap=min_gap_sec, language=language),
            transcript=format_transcript(chunks[0]),
        )
        notes = await llm.generate(prompt, Notes, on_wait)
    else:
        sections: list[Section] = []
        frames: list[FrameSuggestion] = []
        for idx, chunk in enumerate(chunks, start=1):
            await report(f"Writing notes (part {idx} of {len(chunks)})")
            first_ts = int(chunk[0].start)
            last_ts = int(chunks[idx][0].start) - 1 if idx < len(chunks) else max(duration - 1, first_ts)
            share = math.ceil(max_frames * len(chunk) / len(transcript.segments)) + 1
            prompt = _CHUNK_PROMPT.format(
                **common,
                part=idx,
                parts=len(chunks),
                start=format_timestamp(first_ts),
                end=format_timestamp(last_ts),
                max_frames=share,
                frame_rules=_FRAME_RULES.format(first_ts=first_ts, last_ts=last_ts, min_gap=min_gap_sec, language=language),
                transcript=format_transcript(chunk),
            )
            part = await llm.generate(prompt, PartialNotes, on_wait)
            sections.extend(normalize_sections(part.sections, duration))
            frames.extend(part.frames)

        await report("Summarising the whole video")
        outline = "\n".join(
            f"[{format_timestamp(s.start_sec)}] {s.heading}\n" + "\n".join(f"  - {b}" for b in s.bullets)
            for s in sections
        )
        overview = await llm.generate(_REDUCE_PROMPT.format(**common, outline=outline), NotesOverview, on_wait)
        notes = Notes(
            title=overview.title,
            summary=overview.summary,
            key_takeaways=overview.key_takeaways,
            sections=sections,
            frames=frames,
        )

    notes.title = " ".join(notes.title.split())[:200] or meta.title
    notes.sections = normalize_sections(notes.sections, duration)
    notes.key_takeaways = [t.strip() for t in notes.key_takeaways if t.strip()]
    notes.frames = validate_frames(notes.frames, duration, max_frames, min_gap_sec)
    return notes
