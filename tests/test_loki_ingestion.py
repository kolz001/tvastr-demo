from pathlib import Path

from tvastr.config import Settings
from tvastr.domain import Severity
from tvastr.ingestion import SimulatedLogSource, harvest_loki_to_jsonl, loki_entry_to_event
from tvastr.ingestion.loki import default_line_to_event
from tvastr.pipeline import build_pipeline


def test_default_line_to_event_parses_json_log_line() -> None:
    line = '{"level": "error", "service": "svc-a", "message": "ValueError: bad"}'
    event = default_line_to_event(line, labels={"app": "from-label"})
    assert event is not None
    assert event.service == "svc-a"  # JSON service wins over label
    assert event.severity == Severity.ERROR
    assert event.message == "ValueError: bad"


def test_default_line_to_event_falls_back_to_labels() -> None:
    event = default_line_to_event(
        "RuntimeError: boom", labels={"app": "my-app", "level": "warning"}
    )
    assert event is not None
    assert event.service == "my-app"
    assert event.severity == Severity.WARNING
    assert event.message == "RuntimeError: boom"


def test_default_line_to_event_defaults_severity_to_error() -> None:
    # Remediation-focused: when nothing says otherwise, assume ERROR.
    event = default_line_to_event("oops", labels={})
    assert event is not None
    assert event.severity == Severity.ERROR


def test_loki_entry_to_event_applies_timestamp() -> None:
    ts_ns = "1716897600000000000"  # 2024-05-28T11:33:20+00:00
    event = loki_entry_to_event(ts_ns, "ValueError: x", {"app": "svc"})
    assert event is not None
    assert int(event.timestamp.timestamp()) == 1716897600


def test_loki_entry_to_event_skips_when_converter_returns_none() -> None:
    def skip(line: str, labels: dict[str, str]) -> None:
        return None

    assert loki_entry_to_event("0", "x", {}, line_to_event=skip) is None


def test_harvest_loki_writes_jsonl_and_pipeline_can_replay(tmp_path: Path) -> None:
    out = tmp_path / "loki.jsonl"
    entries, events = harvest_loki_to_jsonl(
        url=None,  # forces mock fetcher
        query='{app="llamaindex-app"}',
        out_path=out,
        use_mocks=True,
    )
    assert entries == 3
    assert events == 3
    assert out.exists()

    settings = Settings(use_mocks=True, audit_backend="memory", recurrence_threshold=2)
    run = build_pipeline(settings, log_source=SimulatedLogSource(out)).run()
    assert run.events_ingested == 3
    # Two entries share a ModuleNotFoundError signature → one recurring pattern.
    assert run.patterns_selected >= 1


def test_harvest_without_url_falls_back_to_mock(tmp_path: Path) -> None:
    out = tmp_path / "loki.jsonl"
    entries, events = harvest_loki_to_jsonl(
        url=None, query="{app=\"x\"}", out_path=out, use_mocks=False
    )
    assert entries == 3
    assert events == 3
