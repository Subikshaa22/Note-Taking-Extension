"""Stage registry: which handler runs for which pipeline stage."""
from collections.abc import Awaitable, Callable

from notetaker.domain.status import Stage
from notetaker.pipeline import extract_frames, publish, summarize, transcribe
from notetaker.pipeline.context import StageContext, StageResult

StageHandler = Callable[[StageContext], Awaitable[StageResult]]

HANDLERS: dict[Stage, StageHandler] = {
    Stage.TRANSCRIBE: transcribe.run,
    Stage.SUMMARIZE: summarize.run,
    Stage.EXTRACT_FRAMES: extract_frames.run,
    Stage.PUBLISH: publish.run,
}
