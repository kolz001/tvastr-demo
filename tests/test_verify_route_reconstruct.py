"""Tests for _reconstruct_from_run: extraction of pr_number + pr_files from
a persisted benchmark.compared event.

Task 3 of the verify-source-overlay plan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tvastr.api.routes.verify import _reconstruct_from_run
from tvastr.events import JsonlEventSink, PipelineEvent, run_path


def _emit(sink: JsonlEventSink, type_: str, payload: dict) -> None:  # type: ignore[type-arg]
    sink.emit(
        PipelineEvent(
            type=type_,  # type: ignore[arg-type]
            layer="output",
            step="t",
            run_id="recon-test",
            payload=payload,
        )
    )


def test_reconstruct_extracts_pr_number_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Redirect default_runs_dir so _reconstruct_from_run writes/reads under tmp_path.
    monkeypatch.setattr("tvastr.events.default_runs_dir", lambda: tmp_path)

    rid = "recon-test"
    path = run_path(rid, runs_dir=tmp_path)
    sink = JsonlEventSink(path)
    _emit(sink, "pipeline.start", {"repo": "run-llama/llama_index", "issue_title": "t"})
    _emit(sink, "agent.start", {"fingerprint": rid, "title": "t"})
    _emit(
        sink,
        "fix.generated",
        {"patched_files": {"a/llama_index/x/base.py": "# fix"}, "summary": "s"},
    )
    _emit(
        sink,
        "benchmark.compared",
        {
            "pr_number": 21447,
            "files_both": ["a/llama_index/x/base.py"],
            "files_theirs_only": ["a/llama_index/x/util.py"],
        },
    )

    out = _reconstruct_from_run(rid)
    assert out is not None
    *_, pr_number, pr_files = out
    assert pr_number == 21447
    assert pr_files == ["a/llama_index/x/base.py", "a/llama_index/x/util.py"]


def test_reconstruct_handles_missing_benchmark_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no benchmark.compared event is present, pr_number is None and pr_files is []."""
    monkeypatch.setattr("tvastr.events.default_runs_dir", lambda: tmp_path)

    rid = "recon-no-benchmark"
    path = run_path(rid, runs_dir=tmp_path)
    sink = JsonlEventSink(path)
    _emit(sink, "pipeline.start", {"repo": "run-llama/llama_index", "issue_title": "t"})
    _emit(sink, "agent.start", {"fingerprint": rid, "title": "t"})
    _emit(
        sink,
        "fix.generated",
        {"patched_files": {"a/llama_index/x/base.py": "# fix"}, "summary": "s"},
    )

    out = _reconstruct_from_run(rid)
    assert out is not None
    *_, pr_number, pr_files = out
    assert pr_number is None
    assert pr_files == []


def test_reconstruct_handles_bad_pr_number(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-integer pr_number in the event → pr_number returned as None."""
    monkeypatch.setattr("tvastr.events.default_runs_dir", lambda: tmp_path)

    rid = "recon-bad-pr"
    path = run_path(rid, runs_dir=tmp_path)
    sink = JsonlEventSink(path)
    _emit(sink, "pipeline.start", {"repo": "run-llama/llama_index", "issue_title": "t"})
    _emit(sink, "agent.start", {"fingerprint": rid, "title": "t"})
    _emit(
        sink,
        "fix.generated",
        {"patched_files": {"a/llama_index/x/base.py": "# fix"}, "summary": "s"},
    )
    _emit(
        sink,
        "benchmark.compared",
        {
            "pr_number": "not-a-number",
            "files_both": ["a/llama_index/x/base.py"],
            "files_theirs_only": [],
        },
    )

    out = _reconstruct_from_run(rid)
    assert out is not None
    *_, pr_number, pr_files = out
    assert pr_number is None
    assert pr_files == ["a/llama_index/x/base.py"]
