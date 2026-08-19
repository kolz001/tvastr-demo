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


# ── selflog-axis loop hole: the error log itself must carry self_heal ─────


def test_seam_error_log_forwards_error_payload_keys(tmp_path: Path) -> None:
    """The self-heal wave's error_payload ({"self_heal": True}) must reach the
    ``log.exception("run.failed", ...)`` call itself, not just the persisted
    error event -- otherwise the selflog record for this failure carries no
    self_heal marker and scan.py's selflog guard never fires on it."""
    import tvastr.runner as runner_mod

    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeLog:
        def exception(self, event: str, **kwargs: object) -> None:
            calls.append((event, kwargs))

    monkeypatch_log = runner_mod.log
    runner_mod.log = _FakeLog()
    try:
        path = tmp_path / "seam4.jsonl"

        def _body() -> None:
            raise RuntimeError("boom")

        thread = start_pipeline_thread(
            _body,
            run_id="seam4",
            sink=JsonlEventSink(path),
            error_payload={"self_heal": True, "fingerprint": "abc123def456"},
        )
        thread.join(timeout=10)
    finally:
        runner_mod.log = monkeypatch_log

    assert len(calls) == 1
    event, kwargs = calls[0]
    assert event == "run.failed"
    assert kwargs["run_id"] == "seam4"
    assert kwargs["self_heal"] is True
    assert kwargs["fingerprint"] == "abc123def456"


def test_seam_error_log_without_error_payload_is_byte_identical_to_before(
    tmp_path: Path,
) -> None:
    """The route's call (no ``error_payload``) must log exactly ``run_id`` as
    a kwarg -- the historical shape -- so ``run.py``'s behaviour is unchanged."""
    import tvastr.runner as runner_mod

    calls: list[tuple[str, dict[str, object]]] = []

    class _FakeLog:
        def exception(self, event: str, **kwargs: object) -> None:
            calls.append((event, kwargs))

    original_log = runner_mod.log
    runner_mod.log = _FakeLog()
    try:
        path = tmp_path / "seam5.jsonl"

        def _body() -> None:
            raise RuntimeError("boom")

        thread = start_pipeline_thread(_body, run_id="seam5", sink=JsonlEventSink(path))
        thread.join(timeout=10)
    finally:
        runner_mod.log = original_log

    assert calls == [("run.failed", {"run_id": "seam5"})]
