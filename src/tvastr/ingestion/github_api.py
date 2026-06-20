"""Shared GitHub REST API helpers for the ingestion fetchers."""

from __future__ import annotations

from datetime import datetime

API_ROOT = "https://api.github.com"


def github_headers(token: str | None) -> dict[str, str]:
    """Standard REST headers, with auth when a token is available."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def parse_iso(s: str | None) -> datetime | None:
    """Parse GitHub's ISO-8601 timestamps (``2026-05-20T12:00:00Z``)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
