"""Discover the pull request that addresses an issue, and fetch its diff.

Timeline cross-reference linking is sparse in practice (maintainers rarely
write "closes #N"), so discovery uses the GitHub Search API
(``repo:X type:pr <issue#>``) and ranks candidates: open > merged > closed,
tie-broken by most recently updated. No LLM is involved here — this is the
cheap step that feeds the analysis and comparison layers.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from tvastr.ingestion.github_api import API_ROOT, github_headers
from tvastr.logging import get_logger

log = get_logger(__name__)

_MAX_DIFF_FILES = 30
_MAX_DIFF_LINES = 1500


@dataclass(frozen=True)
class PullRequestRef:
    number: int
    title: str
    state: str  # "open" | "closed"
    merged: bool
    url: str
    changed_files: int = 0


def _rank(item: dict) -> tuple[int, str]:
    """Sort key: lower rank first. open=0, merged=1, closed=2; then -updated."""
    state = item.get("state", "closed")
    merged = bool((item.get("pull_request") or {}).get("merged_at"))
    rank = 0 if state == "open" else (1 if merged else 2)
    return (rank, "-" + str(item.get("updated_at", "")))


# Cache mirrors comments.py: 10-min TTL, bounded, lock-guarded.
_CACHE_TTL_S = 600.0
_CACHE_MAX = 1024
_cache: dict[tuple[str, int], tuple[float, PullRequestRef | None]] = {}
_cache_lock = threading.Lock()


def _cache_get(key):
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
            return True, hit[1]
        if hit:
            del _cache[key]
    return False, None


def _cache_put(key, value):
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            del _cache[min(_cache, key=lambda k: _cache[k][0])]
        _cache[key] = (time.monotonic(), value)


def discover_pr(
    repo: str,
    number: int,
    *,
    token: str | None,
    use_mocks: bool = False,
    transport: object | None = None,
) -> PullRequestRef | None:
    """Find the most relevant PR addressing issue ``number``. None if none/offline."""
    if use_mocks or not token:
        return None

    key = (repo, number)
    found, cached = _cache_get(key)
    if found:
        return cached

    import httpx

    q = f"repo:{repo} type:pr {number}"
    url = f"{API_ROOT}/search/issues"
    try:
        with httpx.Client(timeout=15.0, transport=transport) as client:  # type: ignore[arg-type]
            resp = client.get(url, headers=github_headers(token), params={"q": q, "per_page": "20"})
            resp.raise_for_status()
            items = resp.json().get("items", [])
    except Exception as exc:
        log.warning(
            "analysis.discover_pr.failed",
            repo=repo,
            number=number,
            error=str(exc),
        )
        return None

    if not items:
        _cache_put(key, None)
        return None

    best = sorted(items, key=_rank)[0]
    ref = PullRequestRef(
        number=int(best["number"]),
        title=str(best.get("title", "")),
        state=str(best.get("state", "closed")),
        merged=bool((best.get("pull_request") or {}).get("merged_at")),
        url=str(best.get("html_url", "")),
    )
    _cache_put(key, ref)
    log.info(
        "analysis.discover_pr",
        repo=repo,
        number=number,
        pr=ref.number,
        state=ref.state,
    )
    return ref
