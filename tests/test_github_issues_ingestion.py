from datetime import UTC, datetime
from pathlib import Path

from tvastr.config import Settings
from tvastr.ingestion import (
    IssueRecord,
    SimulatedLogSource,
    harvest_issues_to_jsonl,
    issue_to_events,
)
from tvastr.pipeline import build_pipeline


def _record(number: int, title: str, body: str, labels: list[str] | None = None) -> IssueRecord:
    return IssueRecord(
        number=number,
        title=title,
        body=body,
        created_at=datetime(2026, 5, 20, tzinfo=UTC),
        url=f"https://github.com/x/y/issues/{number}",
        labels=labels or ["bug"],
    )


def test_issue_with_error_signature_becomes_event() -> None:
    rec = _record(1, "Crash", "ValueError: missing 'question'")
    events = issue_to_events(rec, default_service="llama_index")
    assert len(events) == 1
    assert events[0].message.startswith("ValueError:")
    assert events[0].attributes["issue_number"] == "1"
    assert events[0].source == "github_issues"


def test_feature_request_without_error_is_skipped() -> None:
    rec = _record(2, "add streaming support", "Would be nice to support streaming.", ["feature"])
    assert issue_to_events(rec, default_service="llama_index") == []


def test_service_picks_up_topic_label() -> None:
    rec = _record(3, "boom", "RuntimeError: boom", ["bug", "topic:vector_stores"])
    events = issue_to_events(rec, default_service="llama_index")
    assert events[0].service == "vector_stores"


def test_multiple_exception_types_in_one_issue_dedup_by_type() -> None:
    body = (
        "ValueError: x\n"
        "ValueError: x (again, same type)\n"
        "RuntimeError: different beast\n"
    )
    events = issue_to_events(_record(4, "two bugs", body), default_service="svc")
    assert {e.message.split(":", 1)[0] for e in events} == {"ValueError", "RuntimeError"}


def test_harvest_writes_jsonl_and_pipeline_can_replay(tmp_path: Path) -> None:
    out = tmp_path / "issues.jsonl"
    issues, events = harvest_issues_to_jsonl(
        repo="run-llama/llama_index", out_path=out, use_mocks=True, label="bug", limit=10
    )
    assert issues >= 3
    assert events >= 2  # the feature-request issue contributes no events
    assert out.exists() and out.stat().st_size > 0

    settings = Settings(use_mocks=True, audit_backend="memory", recurrence_threshold=2)
    run = build_pipeline(settings, log_source=SimulatedLogSource(out)).run()
    assert run.events_ingested == events
    # The mock fetcher emits two issues sharing a ModuleNotFoundError signature
    # → one recurring pattern.
    assert run.patterns_selected >= 1
