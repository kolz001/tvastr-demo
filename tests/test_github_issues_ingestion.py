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


def test_non_crashing_bug_gets_synthetic_event() -> None:
    """Bug-labeled issues without an exception still produce one event so the
    agent's confidence gate can decide whether to act."""
    rec = _record(
        19293,
        "[Bug]: No Input/Output Token count for Gemini 2.5 models",
        "When using Gemini 2.5, prompt and completion token counts come back as None.",
        ["bug"],
    )
    events = issue_to_events(rec, default_service="llama_index")
    assert len(events) == 1
    assert events[0].message.startswith("UnexpectedBehavior:")
    # The [Bug]: prefix should be stripped from the cleaned title.
    assert "No Input/Output Token count" in events[0].message
    assert events[0].attributes["non_crashing"] == "true"
    assert events[0].attributes["issue_body_excerpt"].startswith("When using Gemini")


def test_bug_titled_without_label_still_produces_synthetic_event() -> None:
    """[Bug]: prefix alone is sufficient; many real repos don't use a bug label."""
    rec = _record(99, "[Bug]: something flaky", "Sometimes the output is wrong.", [])
    events = issue_to_events(rec, default_service="llama_index")
    assert len(events) == 1
    assert events[0].attributes["non_crashing"] == "true"


def test_bug_prefix_requires_word_boundary() -> None:
    """'Buggy …' is not a '[Bug]' prefix — without a label it's skipped, and
    with one the title must survive un-mangled (no 'gy output …')."""
    rec = _record(100, "Buggy output when streaming", "Sometimes wrong.", ["question"])
    assert issue_to_events(rec, default_service="llama_index") == []

    labeled = _record(101, "Buggy output when streaming", "Sometimes wrong.", ["bug"])
    events = issue_to_events(labeled, default_service="llama_index")
    assert len(events) == 1
    assert events[0].message == "UnexpectedBehavior: Buggy output when streaming"


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


def test_search_query_omits_label_qualifier_when_label_empty(monkeypatch) -> None:
    """Regression: an empty label produced a dangling `label:` qualifier that
    GitHub's search matches to nothing — repos with unlabeled issues (e.g.
    perplexityai/bumblebee, 13 open issues, zero labeled) returned []."""
    from tvastr.ingestion.github_issues import GitHubIssuesFetcher

    captured: dict = {}

    class _FakeResp:
        def raise_for_status(self) -> None: ...
        def json(self) -> dict:
            return {"items": []}

    class _FakeClient:
        def __init__(self, *a, **k): ...
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None, params=None):
            captured["params"] = params
            return _FakeResp()

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)
    fetcher = GitHubIssuesFetcher("owner/repo", token="t")

    fetcher.fetch(label="", limit=5, sort="interactions")
    assert "label:" not in captured["params"]["q"]

    fetcher.fetch(label="bug", limit=5, sort="interactions")
    assert 'label:"bug"' in captured["params"]["q"] or "label:bug" in captured["params"]["q"]
