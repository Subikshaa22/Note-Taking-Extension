"""Visual frame selection: choose *when* to capture by looking at the video itself.

The transcript says when something is being discussed, but not whether it is legible on
screen: on a whiteboard lecture the presenter is often standing in front of the very thing
they are explaining, and the board is erased and rewritten several times. So the video is
sampled every few seconds at low resolution and a vision model is shown the whole sequence.
It returns, for every distinct state of the board/slides, the sampled moment where that
content is most complete and least obstructed.

The model only picks timestamps from the samples it was shown. The published images are
captured afterwards, unmodified, from the video at those seconds (Constraint B): nothing is
generated, edited, cropped or composited.
"""
import asyncio
import io

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

from notetaker.domain.models import FrameSuggestion, Notes, VideoMeta, format_timestamp
from notetaker.infra.rate_limiter import WaitCallback
from notetaker.services.frames import ScanFrame
from notetaker.services.llm import LLMClient, PromptPart


class BoardState(BaseModel):
    start_sec: int = Field(description="First sampled second where this state of the board/screen is visible")
    end_sec: int = Field(description="Last sampled second before it is erased, replaced or the video ends")
    content: str = Field(description="What is written or shown in this state, at its most complete")


class VisualPlan(BaseModel):
    # Listed first so the model maps out the board's evolution before choosing frames.
    states: list[BoardState] = Field(default_factory=list)
    frames: list[FrameSuggestion] = Field(default_factory=list)


_INTRO = """You are choosing screenshots for study notes about the video "{title}" ({length}).
Below are frames sampled from the video about every {interval} seconds, in order. Each frame
is preceded by its timestamp. The notes cover these sections:
{outline}

Every frame also has its timestamp printed in its top-left corner (e.g. "t=491s"); always
read the timestamp from the frame itself. That label was added for you and is not part of
the video.

Goal: pick the smallest set of these frames that, together, shows EVERYTHING that is written,
drawn or displayed in the video (whiteboard or blackboard writing, slides, diagrams, tables,
equations, code, on-screen text), each piece of content clearly readable, so a student can
study all of it from the screenshots without watching the video.

First fill `states`: walk through the frames in order and list each distinct state of the
board/screen with its time range and its full content. Then fill `frames` from those states.

How to choose:
- Follow how the board/screen changes over time. Content is added gradually, and is later
  erased, scrolled away or replaced by a new slide. Treat each such state separately.
- For each state, choose the frame where its content is MOST COMPLETE (usually just before
  it is erased or replaced) AND where the presenter blocks the LEAST of it. Strongly prefer
  frames where the presenter stands aside, faces the camera or is out of the shot over frames
  where their body, head or arm covers the writing.
- If no single frame shows a state's content unobstructed, add a second frame of the same
  state in which the previously hidden part is visible (e.g. presenter on the other side).
- Do not pick a frame whose content is fully and clearly visible in another chosen frame.
- Skip frames that show only the presenter talking, a title card or a blank board.
- If nothing is ever written or displayed, return an empty list.
- Return at most {max_frames} frames, in chronological order. timestamp_sec must be exactly
  one of the timestamps listed below.
- caption: 1-3 sentences in {language}. Say what is written or shown (quote the key formulas,
  terms or values you can read) and how it fits the lesson. Do not describe the presenter.

Frames:"""


def build_selection_prompt(
    scan: list[ScanFrame], notes: Notes, meta: VideoMeta, *, interval_sec: int, max_frames: int, language: str
) -> list[PromptPart]:
    outline = "\n".join(f"- [{format_timestamp(s.start_sec)}] {s.heading}" for s in notes.sections) or "- (none)"
    parts: list[PromptPart] = [
        _INTRO.format(
            title=meta.title,
            length=format_timestamp(meta.duration_sec),
            interval=interval_sec,
            outline=outline,
            max_frames=max_frames,
            language=language,
        )
    ]
    for frame in scan:
        parts.append(f"t={frame.timestamp_sec}s ({format_timestamp(frame.timestamp_sec)}):")
        parts.append(label_frame(frame.image, frame.timestamp_sec))
    return parts


def label_frame(image: bytes, timestamp_sec: int) -> bytes:
    """Print the timestamp onto a *scan sample* so the model cannot mix up which image is which.

    Only the low-resolution samples sent to the model are labelled; published frames are
    captured separately and never altered.
    """
    with Image.open(io.BytesIO(image)) as img:
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        text = f"t={timestamp_sec}s"
        size = max(img.height // 12, 12)
        font = _label_font(size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        draw.rectangle([0, 0, right - left + 12, bottom - top + 12], fill="black")
        draw.text((6 - left, 6 - top), text, fill="yellow", font=font)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=80)
        return out.getvalue()


def _label_font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has no sized default font
        return ImageFont.load_default()


def scan_interval(duration_sec: int, interval_sec: int, max_images: int) -> int:
    """Sampling interval that keeps the number of scanned frames within `max_images`."""
    needed = -(-max(duration_sec, 1) // max_images)  # ceiling division
    return max(interval_sec, needed)


def validate_visual_frames(
    frames: list[FrameSuggestion], scanned: list[int], max_frames: int
) -> list[FrameSuggestion]:
    """Keep only frames at timestamps that were actually shown to the model, with a caption.

    A timestamp the model slightly misquoted is snapped to the nearest scanned second if it is
    within half a sampling interval; anything else is dropped rather than guessed.
    """
    if not scanned:
        return []
    ordered = sorted(scanned)
    half_gap = max((b - a for a, b in zip(ordered, ordered[1:])), default=10) / 2
    kept: dict[int, FrameSuggestion] = {}
    for frame in frames:
        caption = " ".join(frame.caption.split())
        if not caption:
            continue
        nearest = min(ordered, key=lambda ts: abs(ts - frame.timestamp_sec))
        if abs(nearest - frame.timestamp_sec) > half_gap or nearest in kept:
            continue
        kept[nearest] = FrameSuggestion(timestamp_sec=nearest, caption=caption)
    result = sorted(kept.values(), key=lambda f: f.timestamp_sec)
    if len(result) <= max_frames:
        return result
    step = (len(result) - 1) / (max_frames - 1) if max_frames > 1 else 0
    return [result[round(i * step)] for i in range(max_frames)]


async def select_frames(
    llm: LLMClient,
    scan: list[ScanFrame],
    notes: Notes,
    meta: VideoMeta,
    *,
    interval_sec: int,
    max_frames: int,
    language: str,
    timeout_sec: float,
    on_wait: WaitCallback | None = None,
) -> list[FrameSuggestion]:
    # Decoding and labelling a few hundred JPEGs takes seconds of CPU: keep it off the event loop.
    parts = await asyncio.to_thread(
        build_selection_prompt, scan, notes, meta, interval_sec=interval_sec, max_frames=max_frames, language=language
    )
    # Medium resolution (~530 tokens per 640x360 sample) keeps board writing legible while
    # costing half of the default; 240 samples then fit well inside a 250k TPM budget.
    plan = await llm.generate_from_parts(parts, VisualPlan, on_wait, timeout_sec, image_resolution="medium")
    return validate_visual_frames(plan.frames, [f.timestamp_sec for f in scan], max_frames)
