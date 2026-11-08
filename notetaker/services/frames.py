"""Real frame capture with ffmpeg (Constraint B: frames are never generated or substituted).

ffmpeg seeks inside the remote stream with HTTP range requests (`-ss` before `-i`), so only
the bytes around each timestamp are downloaded, and writes exactly one decoded frame as JPEG.
"""
import asyncio
import io

from PIL import Image, ImageStat

from notetaker.services.youtube import StreamInfo

# A frame whose luminance barely varies (black/white transition, fade) carries no visual
# information, so it is dropped rather than published with a misleading caption.
_MIN_LUMA_STDDEV = 4.0


class FrameCaptureError(Exception):
    pass


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
    def __init__(self, timeout_sec: float, ffmpeg_bin: str = "ffmpeg"):
        self._timeout = timeout_sec
        self._ffmpeg = ffmpeg_bin

    async def grab(self, stream: StreamInfo, timestamp_sec: int) -> bytes:
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
            raise FrameCaptureError(stderr.decode(errors="replace").strip()[-300:] or f"exit code {proc.returncode}")
        await asyncio.to_thread(check_frame, stdout)
        return stdout
