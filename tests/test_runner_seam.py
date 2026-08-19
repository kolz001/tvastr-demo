"""Tests for the shared pipeline-thread seam (``tvastr.runner``).

``run.py``'s ``_start_pipeline_thread`` and the self-heal fix wave both need
the same three guarantees: register the run in the in-flight map, turn an
escaping exception into a persisted ``error`` event, and deregister on the way
out no matter what. Those guarantees now live in ``tvastr.runner`` and are
pinned here; ``tests/test_run_lifecycle.py`` (unmodified) pins that the API
route's behaviour is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

from tvastr.events import JsonlEventSink
from tvastr.runner import IN_FLIGHT, start_pipeline_thread


def test_seam_registers_and_deregisters_and_runs_body(tmp_path: Path) -> None:
    path = tmp_path / "seam1.jsonl"
    sink = JsonlEventSink(path)
    seen: list[str] = []

    def _body() -> None:
        seen.append("registered" if IN_FLIGHT.get("seam1") is not None else "missing")

    thread = start_pipeline_thread(_body, run_id="seam1", sink=sink)
    thread.join(timeout=10)

    assert seen == ["registered"]
    assert "seam1" not in IN_FLIGHT
    assert thread.daemon is True
    assert thread.name == "tvastr-run-seam1"


def test_seam_converts_exception_into_error_event(tmp_path: Path) -> None:
    path = tmp_path / "seam2.jsonl"
    sink = JsonlEventSink(path)

    def _body() -> None:
        raise RuntimeError("boom")

    thread = start_pipeline_thread(_body, run_id="seam2", sink=sink)
    thread.join(timeout=10)

    assert "seam2" not in IN_FLIGHT
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    assert len(rows) == 1
    assert rows[0]["type"] == "error"
    assert rows[0]["layer"] == "output"
    assert rows[0]["step"] == "pipeline"
    assert rows[0]["run_id"] == "seam2"
    assert rows[0]["payload"] == {"error": "RuntimeError", "message": "boom"}


def test_seam_merges_error_payload_under_the_canonical_keys(tmp_path: Path) -> None:
    """``error_payload`` adds context (the self-heal marker) but must never be
    able to overwrite the failure's own ``error``/``message`` fields."""
    path = tmp_path / "seam3.jsonl"

    def _body() -> None:
        raise ValueError("nope")

    thread = start_pipeline_thread(
        _body,
        run_id="seam3",
        sink=JsonlEventSink(path),
        error_payload={"self_heal": True, "error": "SPOOFED"},
    )
    thread.join(timeout=10)

    payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])["payload"]
    assert payload == {"self_heal": True, "error": "ValueError", "message": "nope"}


def test_route_module_shares_the_same_in_flight_map() -> None:
    """``app.py`` and the stream route read ``run._IN_FLIGHT``; it must be the
    very same dict the seam registers into, not a copy."""
    from tvastr.api.routes import run as run_module

    assert run_module._IN_FLIGHT is IN_FLIGHT
