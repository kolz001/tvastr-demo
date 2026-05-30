"""Liveness / readiness endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from tvastr import __version__
from tvastr.config import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "env": settings.env,
        "mode": "mock" if settings.use_mocks else "live",
    }
