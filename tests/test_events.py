import typing
from pathlib import Path

from tvastr.config import Settings
from tvastr.events import (
    EventType,
    FanoutEventSink,
    JsonlEventSink,
    ListEventSink,
    NullEventSink,
    PipelineEvent,
    list_runs,
    load_events,
    new_run_id,
    run_path,
)
from tvastr.pipeline import build_pipeline


def test_null_sink_drops_silently() -> None:
    sink = NullEventSink()
    sink.emit(PipelineEvent(type="pipeline.start", layer="ingestion", step="x"))
    # No state to assert; just that it doesn't raise.


def test_list_sink_collects_in_order() -> None:
    sink = ListEventSink()
    sink.emit(PipelineEvent(type="pipeline.start", layer="ingestion", step="a"))
    sink.emit(PipelineEvent(type="pipeline.end", layer="ingestion", step="b"))
    assert [e.step for e in sink.events] == ["a", "b"]


def test_jsonl_sink_writes_one_line_per_event(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    sink = JsonlEventSink(path)
    sink.emit(PipelineEvent(type="pipeline.start", layer="ingestion", step="a"))
    sink.emit(PipelineEvent(type="ingest.read", layer="ingestion", step="ingest"))
    assert path.read_text("utf-8").strip().count("\n") == 1  # 2 lines, 1 newline-separated


def test_load_events_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    sink = JsonlEventSink(path)
    sink.emit(PipelineEvent(type="pipeline.start", layer="ingestion", step="a", payload={"x": 1}))
    sink.emit(PipelineEvent(type="pipeline.end", layer="output", step="b"))

    loaded = list(load_events(path))
    assert [(e.type, e.step) for e in loaded] == [("pipeline.start", "a"), ("pipeline.end", "b")]
    assert loaded[0].payload == {"x": 1}


def test_fanout_keeps_going_when_one_sink_raises() -> None:
    class BrokenSink:
        def emit(self, event: PipelineEvent) -> None:
            raise RuntimeError("boom")

    good = ListEventSink()
    fanout = FanoutEventSink(BrokenSink(), good)
    fanout.emit(PipelineEvent(type="pipeline.start", layer="ingestion", step="a"))
    # The broken sink swallowed the event; the good one still saw it.
    assert len(good.events) == 1


def test_pipeline_emits_expected_event_sequence(tmp_path: Path) -> None:
    """End-to-end: run the pipeline with mocks + ListEventSink and check we
    see every layer's signature events at least once."""
    sink = ListEventSink()
    settings = Settings(
        use_mocks=True, audit_backend="memory", dry_run=True, recurrence_threshold=3
    )
    pipeline = build_pipeline(settings, event_sink=sink, run_id="test-run")
    pipeline.run()

    types = [e.type for e in sink.events]
    assert "pipeline.start" in types
    assert "ingest.read" in types
    assert "detect.cluster" in types
    assert "threshold.select" in types
    assert "agent.start" in types
    assert "router.decide" in types
    assert "llm.call" in types
    assert "fix.generated" in types
    assert "pr.drafted" in types
    assert "pr.dry_run" in types
    assert "notify.sent" in types
    assert "audit.saved" in types
    assert "pipeline.end" in types
    # Every event carries the run_id we passed in.
    assert all(e.run_id == "test-run" for e in sink.events)


def test_list_runs_summarises_persisted_streams(tmp_path: Path) -> None:
    run_id = new_run_id()
    path = run_path(run_id, runs_dir=tmp_path)
    sink = JsonlEventSink(path)
    sink.emit(
        PipelineEvent(
            type="pipeline.start",
            layer="ingestion",
            step="open",
            run_id=run_id,
            payload={"repo": "x/y", "issue_number": 42, "issue_title": "Boom"},
        )
    )
    sink.emit(PipelineEvent(type="pr.dry_run", layer="output", step="open_pr", run_id=run_id))
    sink.emit(PipelineEvent(type="pipeline.end", layer="output", step="end", run_id=run_id))

    runs = list_runs(tmp_path)
    assert len(runs) == 1
    summary = runs[0]
    assert summary.run_id == run_id
    assert summary.repo == "x/y"
    assert summary.issue_number == 42
    assert summary.issue_title == "Boom"
    assert summary.outcome == "dry_run"
    assert summary.event_count == 3


def test_benchmark_event_types_exist() -> None:
    args = typing.get_args(EventType)
    assert "benchmark.compared" in args
    assert "benchmark.skipped" in args
