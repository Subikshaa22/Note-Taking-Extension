"""Real frame capture with ffmpeg (Constraint B: frames are never generated or substituted).

ffmpeg seeks inside the remote stream with HTTP range requests (`-ss` before `-i`), so only
the bytes around each timestamp are downloaded, and writes exactly one decoded frame as JPEG.

`scan` reads a low-resolution stream once, start to finish, and samples a frame every few
seconds. Those samples only help *choose* timestamps; the published images are always
captured afterwards by `grab`, unmodified, from the video itself.
"""
import asyncio
import io
import os
import re
import tempfile
from dataclasses import dataclass

from PIL import Image, ImageStat

from notetaker.services.faults import should_inject
from notetaker.services.youtube import StreamInfo

# A frame whose luminance barely varies (black/white transition, fade) carries no visual
# information, so it is dropped rather than published with a misleading caption.
_MIN_LUMA_STDDEV = 4.0


class FrameCaptureError(Exception):
    pass


_URL_RE = re.compile(r"https?://\S+")
_SHOWINFO_PTS = re.compile(r"Parsed_showinfo.*?pts_time:\s*(-?[\d.]+)")


@dataclass(frozen=True)
class ScanFrame:
    timestamp_sec: int
    image: bytes


def parse_showinfo_times(stderr: str) -> list[float]:
    """The presentation time of every frame that reached ffmpeg's showinfo filter, in order."""
    return [float(m.group(1)) for m in _SHOWINFO_PTS.finditer(stderr)]


def ffmpeg_error_text(stderr: bytes) -> str:
    """ffmpeg prefixes errors with the (very long, signed) input URL; drop it so the
    actual reason survives truncation and no stream signature ends up in job warnings."""
    text = _URL_RE.sub("<stream>", stderr.decode(errors="replace")).strip()
    return text[-300:]


def check_frame(image_bytes: bytes) -> tuple[int, int]:
    """Validate a captured JPEG; returns its (width, height)."""
    if not image_bytes:
        raise FrameCaptureError("ffmpeg produced no image")
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            img.load()
            size = img.size
            stddev = ImageStat.Stat(img.convert("L")).stddev[0]
    except (OSError, ValueError) as exc:
        raise FrameCaptureError(f"captured data is not a valid image: {exc}") from exc
    if stddev < _MIN_LUMA_STDDEV:
        raise FrameCaptureError("captured frame is blank (solid colour)")
    return size


class FrameGrabber:
    def __init__(self, timeout_sec: float, ffmpeg_bin: str = "ffmpeg", failure_rate: float = 0.0):
        self._timeout = timeout_sec
        self._ffmpeg = ffmpeg_bin
        self._failure_rate = failure_rate

    async def scan(self, stream: StreamInfo, interval_sec: float, height: int, timeout_sec: float) -> list[ScanFrame]:
        """Sample one frame at least every `interval_sec` seconds across the whole video.

        `select` keeps the first frame at or after each interval boundary and `showinfo`
        reports its exact time, so each sample is tied to the second it was really shown.
        """
        headers = "".join(f"{k}: {v}\r\n" for k, v in stream.http_headers.items())
        gap = max(interval_sec - 0.05, 0.1)
        # The comma inside gte() is escaped so ffmpeg does not read it as a filter separator.
        vf = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{gap})',showinfo,scale=-2:{height}"
        with tempfile.TemporaryDirectory(prefix="scan-") as tmp:
            args = [
                self._ffmpeg,
                "-hide_banner",
                "-nostdin",
                *(["-headers", headers] if headers else []),
                "-i",
                stream.url,
                "-an",
                "-vf",
                vf,
                "-fps_mode",
                "vfr",
                "-q:v",
                "4",
                os.path.join(tmp, "%05d.jpg"),
            ]
            try:
                proc = await asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
                )
            except FileNotFoundError as exc:
                raise FrameCaptureError("ffmpeg is not installed") from exc
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
            except asyncio.TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise FrameCaptureError(f"video scan timed out after {timeout_sec:.0f}s") from exc
            except asyncio.CancelledError:
                # A scan started ahead of time (during summarize) is cancelled if that stage fails.
                proc.kill()
                await proc.wait()
                raise
            text = stderr.decode(errors="replace")
            if proc.returncode != 0:
                raise FrameCaptureError(ffmpeg_error_text(stderr) or f"exit code {proc.returncode}")
            times = parse_showinfo_times(text)
            files = sorted(f for f in os.listdir(tmp) if f.endswith(".jpg"))
            frames: list[ScanFrame] = []
            seen: set[int] = set()
            for name, pts in zip(files, times):
                ts = max(int(round(pts)), 0)
                if ts in seen:
                    continue
                seen.add(ts)
                with open(os.path.join(tmp, name), "rb") as fh:
                    frames.append(ScanFrame(ts, fh.read()))
        if not frames:
            raise FrameCaptureError("video scan produced no frames")
        return frames

    async def grab(self, stream: StreamInfo, timestamp_sec: int) -> bytes:
        if should_inject(self._failure_rate, f"frame failure at {timestamp_sec}s"):
            raise FrameCaptureError("simulated capture failure")
        headers = "".join(f"{k}: {v}\r\n" for k, v in stream.http_headers.items())
        args = [
            self._ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            *(["-headers", headers] if headers else []),
            "-ss",
            str(timestamp_sec),
            "-i",
            stream.url,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            "-f",
            "image2",
            "-c:v",
            "mjpeg",
            "pipe:1",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except FileNotFoundError as exc:
            raise FrameCaptureError("ffmpeg is not installed") from exc
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except asyncio.TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise FrameCaptureError(f"timed out after {self._timeout:.0f}s") from exc
        if proc.returncode != 0:
            raise FrameCaptureError(ffmpeg_error_text(stderr) or f"exit code {proc.returncode}")
        await asyncio.to_thread(check_frame, stdout)
        return stdout
