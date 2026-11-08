"""Job lifecycle: statuses, pipeline stages and the allowed state transitions (spec §4)."""
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    TRANSCRIBING = "TRANSCRIBING"
    SUMMARIZING = "SUMMARIZING"
    EXTRACTING_FRAMES = "EXTRACTING_FRAMES"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATUSES


TERMINAL_STATUSES = frozenset(
    {
        JobStatus.COMPLETED,
        JobStatus.COMPLETED_WITH_WARNINGS,
        JobStatus.FAILED,
        JobStatus.DEAD_LETTERED,
    }
)


class Stage(StrEnum):
    """A unit of pipeline work. Each stage travels through the broker as its own message."""

    TRANSCRIBE = "transcribe"
    SUMMARIZE = "summarize"
    EXTRACT_FRAMES = "extract_frames"
    PUBLISH = "publish"

    @property
    def status(self) -> JobStatus:
        return _STAGE_STATUS[self]

    @property
    def next(self) -> "Stage | None":
        order = list(Stage)
        idx = order.index(self)
        return order[idx + 1] if idx + 1 < len(order) else None

    def accepts(self, status: JobStatus) -> bool:
        """Whether a message for this stage should run when the job is in `status`.

        Anything else means the message is stale (a duplicate delivery of a stage that
        already finished) and is acknowledged without doing work.
        """
        if status == self.status:
            return True
        return self is Stage.TRANSCRIBE and status == JobStatus.QUEUED


_STAGE_STATUS = {
    Stage.TRANSCRIBE: JobStatus.TRANSCRIBING,
    Stage.SUMMARIZE: JobStatus.SUMMARIZING,
    Stage.EXTRACT_FRAMES: JobStatus.EXTRACTING_FRAMES,
    Stage.PUBLISH: JobStatus.PUBLISHING,
}

_NON_TERMINAL = [s for s in JobStatus if not s.is_terminal]

ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.TRANSCRIBING}),
    JobStatus.TRANSCRIBING: frozenset({JobStatus.SUMMARIZING}),
    JobStatus.SUMMARIZING: frozenset({JobStatus.EXTRACTING_FRAMES}),
    JobStatus.EXTRACTING_FRAMES: frozenset({JobStatus.PUBLISHING}),
    JobStatus.PUBLISHING: frozenset({JobStatus.COMPLETED, JobStatus.COMPLETED_WITH_WARNINGS}),
}
for _status in _NON_TERMINAL:
    ALLOWED_TRANSITIONS[_status] = ALLOWED_TRANSITIONS[_status] | {
        JobStatus.FAILED,
        JobStatus.DEAD_LETTERED,
    }


def allowed_sources(target: JobStatus) -> list[JobStatus]:
    """Statuses from which `target` may be entered."""
    return [src for src, dsts in ALLOWED_TRANSITIONS.items() if target in dsts]


def can_transition(src: JobStatus, dst: JobStatus) -> bool:
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


# Coarse progress shown while a stage is running; stages refine it as they go.
STATUS_PROGRESS = {
    JobStatus.QUEUED: 0,
    JobStatus.TRANSCRIBING: 5,
    JobStatus.SUMMARIZING: 25,
    JobStatus.EXTRACTING_FRAMES: 55,
    JobStatus.PUBLISHING: 85,
    JobStatus.COMPLETED: 100,
    JobStatus.COMPLETED_WITH_WARNINGS: 100,
}
