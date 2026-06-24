"""FastAPI application factory."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from tvastr import __version__
from tvastr.api.routes import health, issues, pr, remediate, run, verify
from tvastr.config import get_settings
from tvastr.logging import configure_logging


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

    @app.get("/app", response_class=HTMLResponse, include_in_schema=False)
    def _app_page() -> HTMLResponse:
        page = Path(__file__).resolve().parent / "templates" / "app.html"
        return HTMLResponse(page.read_text(encoding="utf-8"))

    return app
