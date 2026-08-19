"""The shared pipeline-thread seam: run a pipeline body in a background thread.

Two callers need *exactly* the same three guarantees around a pipeline run:

* the run is registered in a process-wide in-flight map while it executes, so
  the stream endpoint can tell "still producing events" from "finished", and
  the startup sweep never stamps a live run ``pipeline.interrupted``;
* an exception escaping the body becomes a persisted ``error`` event on the
  run's own JSONL (a terminal event — the stream drains and the run reads back
  as failed, instead of hanging as a run that never ended);
* the registry entry is removed on the way out, no matter what.

The callers are ``api/routes/run.py`` (a user picking an issue in the UI) and
``selfheal/remediate.py`` (the weekly self-remediation wave). Everything else
about those two — how events are obtained, which settings the run uses — is
caller-specific and deliberately stays with the caller; only the thread
mechanics live here.

``IN_FLIGHT`` is module-level, and ``run.py`` aliases it as ``_IN_FLIGHT``
rather than keeping its own dict: ``api/app.py``'s sweep and the stream route
read that name, and there must be exactly ONE map or a self-heal run would be
invisible to both.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from tvastr.events import EventSink, PipelineEvent
from tvastr.logging import get_logger

log = get_logger(__name__)

# Live pipeline threads by run_id. Entries remove themselves when the thread
# finishes, so "in the dict and alive" ⇔ the run is still producing events.
IN_FLIGHT: dict[str, threading.Thread] = {}


def start_pipeline_thread(
    body: Callable[[], None],
    *,
    run_id: str,
    sink: EventSink,
    error_payload: dict[str, Any] | None = None,
) -> threading.Thread:
    """Run ``body`` in a daemon thread, persisting a failure via ``sink``.

    Returns the (already started) thread so callers that need to run
    sequentially — the self-heal fix wave — can join it.

    ``error_payload`` is merged UNDER the canonical ``error``/``message`` keys
    of the failure event. It exists for the self-heal wave: if the body dies
    before ``pipeline.run`` emits ``pipeline.start`` (say ``build_pipeline``
    itself raises), the run file would otherwise carry an ``error`` event and
    no ``self_heal`` marker anywhere — and the next daily scan would mine the
    wave's own crash as a fresh failure to fix. Passing ``{"self_heal": True}``
    keeps ``scan.py``'s guard (which checks EVERY event's payload) effective
    even for a run that never really started.
    """

    def _run() -> None:
        try:
            body()
        except Exception as exc:
            log.exception("run.failed", **{"run_id": run_id, **(error_payload or {})})
            sink.emit(
                PipelineEvent(
                    type="error",
                    layer="output",
                    step="pipeline",
                    run_id=run_id,
                    payload={
                        **(error_payload or {}),
                        "error": type(exc).__name__,
                        "message": str(exc),
                    },
                )
            )
        finally:
            IN_FLIGHT.pop(run_id, None)

    t = threading.Thread(target=_run, daemon=True, name=f"tvastr-run-{run_id}")
    IN_FLIGHT[run_id] = t
    t.start()
    return t
