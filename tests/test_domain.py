import pytest

from notetaker.config import Settings
from notetaker.domain.errors import ErrorCode, FatalError
from notetaker.domain.identifiers import canonical_video_url, parse_notion_id, parse_video_id
from notetaker.domain.models import format_timestamp
from notetaker.domain.status import JobStatus, Stage, allowed_sources, can_transition


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s&list=PL123",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ?si=abc",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://www.youtube.com/embed/dQw4w9WgXcQ",
        "https://www.youtube.com/live/dQw4w9WgXcQ",
        "youtube.com/watch?v=dQw4w9WgXcQ",
    ],
)
def test_parse_video_id_accepts_common_forms(url):
    assert parse_video_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://vimeo.com/123",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/channel/UC123",
        "ftp://youtube.com/watch?v=dQw4w9WgXcQ",
        "https://evil.com/watch?v=dQw4w9WgXcQ",
    ],
)
def test_parse_video_id_rejects_invalid(url):
    with pytest.raises(FatalError) as exc:
        parse_video_id(url)
    assert exc.value.code == ErrorCode.INVALID_URL


def test_canonical_url_with_timestamp():
    assert canonical_video_url("dQw4w9WgXcQ", 61) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=61s"


@pytest.mark.parametrize(
    "value",
    [
        "0123456789abcdef0123456789abcdef",
        "01234567-89ab-cdef-0123-456789abcdef",
        "https://www.notion.so/team/My-Page-0123456789abcdef0123456789abcdef",
        "https://www.notion.so/My-Deadbeef-Page-0123456789abcdef0123456789abcdef?pvs=4",
        "https://www.notion.so/0123456789ABCDEF0123456789ABCDEF",
    ],
)
def test_parse_notion_id(value):
    assert parse_notion_id(value) == "01234567-89ab-cdef-0123-456789abcdef"


@pytest.mark.parametrize("value", ["", "not-an-id", "https://www.notion.so/My-Page"])
def test_parse_notion_id_rejects_invalid(value):
    with pytest.raises(FatalError) as exc:
        parse_notion_id(value)
    assert exc.value.code == ErrorCode.INVALID_NOTION_PARENT_ID


def test_stage_chain_matches_spec_order():
    chain = [Stage.TRANSCRIBE]
    while chain[-1].next:
        chain.append(chain[-1].next)
    assert [s.status for s in chain] == [
        JobStatus.TRANSCRIBING,
        JobStatus.SUMMARIZING,
        JobStatus.EXTRACTING_FRAMES,
        JobStatus.PUBLISHING,
    ]


def test_stage_accepts_only_current_status():
    assert Stage.TRANSCRIBE.accepts(JobStatus.QUEUED)
    assert Stage.TRANSCRIBE.accepts(JobStatus.TRANSCRIBING)
    assert not Stage.TRANSCRIBE.accepts(JobStatus.SUMMARIZING)
    assert Stage.PUBLISH.accepts(JobStatus.PUBLISHING)
    assert not Stage.SUMMARIZE.accepts(JobStatus.COMPLETED)


def test_transitions():
    assert can_transition(JobStatus.QUEUED, JobStatus.TRANSCRIBING)
    assert not can_transition(JobStatus.QUEUED, JobStatus.SUMMARIZING)
    assert can_transition(JobStatus.PUBLISHING, JobStatus.COMPLETED_WITH_WARNINGS)
    assert can_transition(JobStatus.EXTRACTING_FRAMES, JobStatus.DEAD_LETTERED)
    for terminal in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.DEAD_LETTERED):
        assert terminal.is_terminal
        assert not can_transition(terminal, JobStatus.FAILED)
    assert set(allowed_sources(JobStatus.FAILED)) == {
        s for s in JobStatus if not s.is_terminal
    }


def test_format_timestamp():
    assert format_timestamp(0) == "00:00"
    assert format_timestamp(461.7) == "07:41"
    assert format_timestamp(3725) == "1:02:05"


def test_settings_parse_csv_env(monkeypatch):
    monkeypatch.setenv("RETRY_DELAYS_SEC", "2, 4,8")
    monkeypatch.setenv("TRANSCRIPT_LANGUAGES", "en,de")
    settings = Settings(_env_file=None)
    assert settings.retry_delays_sec == [2, 4, 8]
    assert settings.transcript_languages == ["en", "de"]
    assert [settings.retry_delay_for(n) for n in (1, 2, 3, 4, 9)] == [2, 4, 8, 8, 8]


def test_default_notion_parent_accepts_a_pasted_link(monkeypatch):
    monkeypatch.setenv(
        "NOTION_DEFAULT_PARENT_ID",
        "https://www.notion.so/myspace/Study-Notes-0123456789abcdef0123456789abcdef?source=copy_link",
    )
    assert Settings(_env_file=None).notion_default_parent_id == "01234567-89ab-cdef-0123-456789abcdef"
    monkeypatch.setenv("NOTION_DEFAULT_PARENT_ID", "")
    assert Settings(_env_file=None).notion_default_parent_id == ""
    monkeypatch.setenv("NOTION_DEFAULT_PARENT_ID", "not a notion id")
    with pytest.raises(ValueError):
        Settings(_env_file=None)
