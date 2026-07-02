"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from tvastr import __version__
from tvastr.api.routes import health, issues, pr, remediate, run, verify
from tvastr.config import get_settings
from tvastr.logging import configure_logging, get_logger

log = get_logger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

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

    from tvastr.api.routes.run import _IN_FLIGHT
    from tvastr.events import default_runs_dir, mark_interrupted_runs

    runs_dir = default_runs_dir()
    if runs_dir.is_dir():
        marked = mark_interrupted_runs(runs_dir, _IN_FLIGHT)
        if marked:
            log.info("app.sweep.marked_interrupted", count=marked)

    @app.get("/app", response_class=HTMLResponse, include_in_schema=False)
    def _app_page() -> HTMLResponse:
        page = Path(__file__).resolve().parent / "templates" / "app.html"
        return HTMLResponse(page.read_text(encoding="utf-8"))

    return app
