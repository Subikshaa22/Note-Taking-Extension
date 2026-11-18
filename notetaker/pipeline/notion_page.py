"""Pure construction of the Notion page body (headings, bullets, captioned frame images).

Layout:
  callout   source video link, duration, job id (the job id doubles as an idempotency
            marker used to find a page created by a crashed worker)
  Summary / Key takeaways
  Notes     one heading per section (linked to that moment in the video), its bullets,
            then the frames that fall inside the section, each preceded by a line with its
            timestamp (linked to that moment) and captioned with timestamp + description
"""
from dataclasses import dataclass

from notetaker.domain.identifiers import canonical_video_url
from notetaker.domain.models import Notes, Section, VideoMeta, format_timestamp

MAX_TEXT = 2000  # Notion's limit per rich-text object
MAX_BLOCKS_PER_REQUEST = 100


@dataclass(frozen=True)
class PlacedFrame:
    timestamp_sec: int
    caption: str
    upload_id: str


def rich_text(text: str, link: str | None = None, *, bold: bool = False, italic: bool = False, color: str = "default") -> list[dict]:
    pieces = [text[i : i + MAX_TEXT] for i in range(0, len(text), MAX_TEXT)] or [""]
    return [
        {
            "type": "text",
            "text": {"content": piece, **({"link": {"url": link}} if link else {})},
            "annotations": {"bold": bold, "italic": italic, "color": color},
        }
        for piece in pieces
    ]


def _block(kind: str, **payload) -> dict:
    return {"object": "block", "type": kind, kind: payload}


def heading(level: int, parts: list[dict]) -> dict:
    return _block(f"heading_{level}", rich_text=parts)


def paragraph(parts: list[dict]) -> dict:
    return _block("paragraph", rich_text=parts)


def bullet(text: str) -> dict:
    return _block("bulleted_list_item", rich_text=rich_text(text))


def callout(parts: list[dict], emoji: str) -> dict:
    return _block("callout", rich_text=parts, icon={"type": "emoji", "emoji": emoji}, color="gray_background")


def image(upload_id: str, caption: list[dict]) -> dict:
    return _block("image", type="file_upload", file_upload={"id": upload_id}, caption=caption)


def divider() -> dict:
    return _block("divider")


def job_marker(job_id: str) -> str:
    return f"Job ID: {job_id}"


def _frame_blocks(frame: PlacedFrame, video_id: str) -> list[dict]:
    """A visible timestamp line (linked to that second of the video), then the image itself,
    whose caption repeats the timestamp so it stays with the image when copied."""
    stamp = format_timestamp(frame.timestamp_sec)
    link = canonical_video_url(video_id, frame.timestamp_sec)
    label = paragraph(rich_text("🕒 Frame at ") + rich_text(stamp, link, bold=True) + rich_text(" · real frame from the video", color="gray"))
    caption = rich_text(stamp, link, bold=True) + rich_text(f" — {frame.caption}")
    return [label, image(frame.upload_id, caption)]


def _frames_by_section(sections: list[Section], frames: list[PlacedFrame]) -> list[list[PlacedFrame]]:
    """Assign each frame to the last section starting at or before it (first section otherwise)."""
    buckets: list[list[PlacedFrame]] = [[] for _ in sections]
    for frame in sorted(frames, key=lambda f: f.timestamp_sec):
        idx = 0
        for i, section in enumerate(sections):
            if section.start_sec <= frame.timestamp_sec:
                idx = i
        buckets[idx].append(frame)
    return buckets


def build_page_blocks(notes: Notes, meta: VideoMeta, frames: list[PlacedFrame], job_id: str) -> list[dict]:
    video_url = canonical_video_url(meta.video_id)
    blocks: list[dict] = [
        callout(
            rich_text("Source: ")
            + rich_text(meta.title, video_url, bold=True)
            + rich_text(f" · {format_timestamp(meta.duration_sec)}")
            + (rich_text(f" · {meta.channel}") if meta.channel else [])
            + rich_text(f"\n{job_marker(job_id)}", color="gray"),
            "📺",
        )
    ]

    if notes.summary:
        blocks += [heading(2, rich_text("Summary")), paragraph(rich_text(notes.summary))]
    if notes.key_takeaways:
        blocks.append(heading(2, rich_text("Key takeaways")))
        blocks += [bullet(t) for t in notes.key_takeaways]

    if notes.sections:
        blocks.append(heading(2, rich_text("Notes")))
        for section, section_frames in zip(notes.sections, _frames_by_section(notes.sections, frames)):
            blocks.append(
                heading(
                    3,
                    rich_text(f"{section.heading} ")
                    + rich_text(
                        f"({format_timestamp(section.start_sec)})",
                        canonical_video_url(meta.video_id, section.start_sec),
                        color="gray",
                    ),
                )
            )
            blocks += [bullet(b) for b in section.bullets]
            for f in section_frames:
                blocks += _frame_blocks(f, meta.video_id)
    elif frames:
        blocks.append(heading(2, rich_text("Key visuals")))
        for f in sorted(frames, key=lambda f: f.timestamp_sec):
            blocks += _frame_blocks(f, meta.video_id)

    return blocks


def page_title_property(title: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": title[:200]}}]}
