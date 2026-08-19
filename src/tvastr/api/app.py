"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from tvastr import __version__
from tvastr.api.routes import health, issues, pr, remediate, run, verify
from tvastr.api.routes.run import _IN_FLIGHT
from tvastr.config import get_settings
from tvastr.events import default_runs_dir, mark_interrupted_runs
from tvastr.logging import configure_logging, get_logger
from tvastr.selfheal.scheduler import SelfHealScheduler

log = get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        json_output=settings.log_json,
        selflog_dir=Path("data/selflogs") if settings.self_heal_enabled else None,
        retention_days=settings.self_heal_retention_days,
    )

    app = FastAPI(
        title="tvastr",
        version=__version__,
        summary="Autonomous Code Remediation Agent",
        description=(
            "Monitors application logs for recurring failures, traces root causes in a "
            "GitHub repo, and opens fix PRs. Hybrid local/cloud LLM routing keeps "
            "sensitive data on the local boundary."
        ),
    )
    app.include_router(health.router)
    app.include_router(issues.router)
    app.include_router(pr.router)
    app.include_router(remediate.router)
    app.include_router(run.router)
    app.include_router(verify.router)

    # Single-process-scoped: liveness is judged against this process's
    # in-memory _IN_FLIGHT map, so a sibling worker's still-running run would
    # look "non-terminal" here too — a multi-worker deployment needs a shared
    # liveness signal before any worker can safely run this sweep. Files with
    # zero parseable events are deliberately skipped (mark_interrupted_runs
    # treats "no last event" the same as "nothing to mark terminal/non-terminal
    # about" — there's no in-progress run to have been interrupted). Since the
    # POST handler now pre-creates the run file before starting its thread, a
    # crash-before-first-emit also leaves a zero-event file here — same skip.
    if settings.sweep_on_startup:
        runs_dir = default_runs_dir()
        if runs_dir.is_dir():
            marked = mark_interrupted_runs(runs_dir, _IN_FLIGHT)
            if marked:
                log.info("app.sweep.marked_interrupted", count=marked)

    # Same single-scheduler rationale as the sweep gate above: this machine's
    # ./data is shared between the host dev process and the docker-compose
    # container, so only one process may run the self-heal scheduler against
    # it. Store the scheduler OBJECT (not just its thread) on app.state so
    # Task 6's status route can call .status() on it.
    if settings.self_heal_enabled:
        scheduler = SelfHealScheduler(settings=settings)
        scheduler.start()
        app.state.selfheal_scheduler = scheduler

    @app.get("/app", response_class=HTMLResponse, include_in_schema=False)
    def _app_page() -> HTMLResponse:
        page = Path(__file__).resolve().parent / "templates" / "app.html"
        return HTMLResponse(page.read_text(encoding="utf-8"))

    return app
