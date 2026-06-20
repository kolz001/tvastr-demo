"""GitHub issue comment helpers: fetch + resolution detection.

An issue can be open in GitHub but effectively resolved by a maintainer
comment ("fixed in v0.10.42", "closed by #19500", "duplicate of #18900").
This module fetches the last few comments of an issue and applies a small
keyword detector so the triage UI can warn the user *before* they burn a
Claude run on a dead issue.

Design:
- The detector is regex + author-association based (no LLM call). Cheap and
  honest — false positives are surfaced as "possibly resolved" rather than
  "definitely resolved."
- Maintainer comments (``MEMBER`` / ``OWNER`` / ``COLLABORATOR``) carry more
  weight than random-user comments. Bot comments (``stale[bot]``,
  ``github-actions[bot]``) are filtered out before signal extraction.
- One assessment per issue: confidence ∈ ``high|medium|low|none``, a short
  human label for the UI chip, and the matched signal phrases for a tooltip.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from tvastr.ingestion.github_api import API_ROOT, github_headers, parse_iso
from tvastr.logging import get_logger

log = get_logger(__name__)


Confidence = Literal["high", "medium", "low", "none"]


@dataclass(frozen=True)
class CommentSnippet:
    author: str
    author_association: str  # OWNER / MEMBER / COLLABORATOR / CONTRIBUTOR / NONE
    body: str
    created_at: datetime
    is_bot: bool

    @property
    def is_maintainer(self) -> bool:
        return self.author_association.upper() in {"OWNER", "MEMBER", "COLLABORATOR"}


@dataclass(frozen=True)
class ResolutionAssessment:
    confidence: Confidence
    label: str
    signals: list[str] = field(default_factory=list)
    comment_count: int = 0
    days_since_last_comment: int | None = None


# --- Detector --------------------------------------------------------------

# Each tuple: (regex, short human label). Compiled lazily for clarity.
_RESOLVED_PATTERNS: list[tuple[str, str]] = [
    # "fixed/resolved/addressed in <version-or-PR>"
    (r"\bfixed in\b\s+v?[\d\.]+", "fixed in version"),
    (r"\bresolved in\b\s+v?[\d\.]+", "resolved in version"),
    (r"\breleased in\b\s+v?[\d\.]+", "released in version"),
    (r"\bavailable in\b\s+v?[\d\.]+", "available in version"),
    (r"\bfixed in\b\s+(?:PR\s+)?#\d+", "fixed in PR"),
    (r"\bresolved in\b\s+(?:PR\s+)?#\d+", "resolved in PR"),
    (r"\baddressed in\b\s+(?:PR\s+)?#\d+", "addressed in PR"),
    (r"\blanded in\b\s+(?:PR\s+)?#\d+", "landed in PR"),
    # Explicit close/merge references. Note: a bare "see #N" is deliberately
    # NOT a signal — cross-referencing issues is everyday GitHub usage and
    # almost never means "resolved".
    (r"\bclosed by\b\s+#\d+", "closed by PR"),
    (r"\bmerged in\b\s+#\d+", "merged in PR"),
    (r"\bduplicate of\b\s+#\d+", "duplicate of"),
    # Free-form "this is fixed" forms. Allow up to ~30 chars of hedging
    # between subject and verb ("this appears to have been fixed",
    # "this seems to be resolved").
    (
        r"\bthis\b[^.\n]{0,30}\b"
        r"(?:was|has been|appears to (?:have been|be)|seems? to (?:have been|be)|is)\s+"
        r"(?:fixed|resolved|addressed)\b",
        "fixed (asserted)",
    ),
    (r"\bshould (?:now )?be (?:resolved|fixed)\b", "should be fixed"),
    (r"\bworks (?:for me|fine) (?:now|on (?:the )?latest)\b", "works now"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), label) for p, label in _RESOLVED_PATTERNS]

_BOT_LOGIN_RE = re.compile(r"\[bot\]$|^github-actions$|^stale$", re.IGNORECASE)


def _is_bot_user(login: str, user_type: str | None) -> bool:
    if (user_type or "").lower() == "bot":
        return True
    return bool(_BOT_LOGIN_RE.search(login))


def _scan(body: str) -> list[str]:
    """Return the labels of all resolution patterns matched in ``body``."""
    seen: set[str] = set()
    found: list[str] = []
    for rx, label in _COMPILED:
        if rx.search(body) and label not in seen:
            seen.add(label)
            found.append(label)
    return found


def _days_since(ts: datetime) -> int:
    now = datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0, (now - ts).days)


_STALE_DAYS = 180


def detect_resolution(
    comments: list[CommentSnippet], issue_body: str | None = None
) -> ResolutionAssessment:
    """Apply the detector to a comment list (+ optional body). Honest verdicts.

    Confidence ladder:
      - ``high``   — maintainer comment with a "fixed in X" / "closed by #N" signal.
      - ``medium`` — any non-bot comment with a resolved-pattern match.
      - ``low``    — many comments but no recent activity (likely stale).
      - ``none``   — nothing notable; don't show a chip.
    """
    human_comments = [c for c in comments if not c.is_bot]
    body_signals = _scan(issue_body or "")

    maintainer_signals: list[str] = []
    any_signals: list[str] = list(body_signals)
    for c in human_comments:
        hits = _scan(c.body)
        any_signals.extend(hits)
        if c.is_maintainer:
            maintainer_signals.extend(hits)

    # Dedupe across sources.
    def _dedupe(xs: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    maintainer_signals = _dedupe(maintainer_signals)
    any_signals = _dedupe(any_signals)

    last_at = max((c.created_at for c in human_comments), default=None)
    days_since = _days_since(last_at) if last_at else None
    comment_count = len(comments)

    if maintainer_signals:
        return ResolutionAssessment(
            confidence="high",
            label=f"possibly resolved · {maintainer_signals[0]}",
            signals=maintainer_signals,
            comment_count=comment_count,
            days_since_last_comment=days_since,
        )

    if any_signals:
        return ResolutionAssessment(
            confidence="medium",
            label=f"possibly resolved · {any_signals[0]}",
            signals=any_signals,
            comment_count=comment_count,
            days_since_last_comment=days_since,
        )

    if comment_count >= 3 and days_since is not None and days_since >= _STALE_DAYS:
        return ResolutionAssessment(
            confidence="low",
            label=f"stale · last comment {days_since}d ago",
            signals=[f"no activity in {days_since} days"],
            comment_count=comment_count,
            days_since_last_comment=days_since,
        )

    return ResolutionAssessment(
        confidence="none",
        label="",
        signals=[],
        comment_count=comment_count,
        days_since_last_comment=days_since,
    )


# --- Fetcher ---------------------------------------------------------------


def fetch_recent_comments(
    repo: str,
    number: int,
    *,
    token: str | None,
    limit: int = 10,
    transport: object | None = None,
) -> list[CommentSnippet]:
    """Fetch the most recent ``limit`` comments via GitHub's REST API.

    The per-issue comments endpoint only returns ascending order (it has no
    ``sort``/``direction`` params — those belong to the repo-level endpoint),
    so to get the *most recent* comments we page to the end: one call covers
    threads up to 100 comments; longer threads cost one extra call for the
    ``rel="last"`` page (plus one more when that page alone can't fill
    ``limit``).

    ``transport`` lets tests inject an ``httpx.MockTransport``.
    """
    import httpx

    headers = github_headers(token)
    url = f"{API_ROOT}/repos/{repo}/issues/{number}/comments"

    log.info("ingest.comments.fetch", repo=repo, number=number, limit=limit)
    with httpx.Client(timeout=15.0, transport=transport) as client:  # type: ignore[arg-type]
        resp = client.get(url, headers=headers, params={"per_page": "100"})
        resp.raise_for_status()
        items = resp.json()
        if last := resp.links.get("last"):
            resp = client.get(last["url"], headers=headers)
            resp.raise_for_status()
            items = resp.json()
            if len(items) < limit and (prev := resp.links.get("prev")):
                prev_resp = client.get(prev["url"], headers=headers)
                prev_resp.raise_for_status()
                items = prev_resp.json() + items

    out: list[CommentSnippet] = []
    for item in items[-limit:]:
        user = item.get("user") or {}
        login = str(user.get("login", ""))
        out.append(
            CommentSnippet(
                author=login,
                author_association=str(item.get("author_association", "NONE")),
                body=str(item.get("body") or ""),
                created_at=parse_iso(item.get("created_at")) or datetime.now(UTC),
                is_bot=_is_bot_user(login, user.get("type")),
            )
        )
    return out


def fetch_issue_body(
    repo: str, number: int, *, token: str | None, transport: object | None = None
) -> str | None:
    """Fetch an issue's body — it can carry resolution signals too
    ("Update: fixed in v0.10.42, leaving open for tracking")."""
    import httpx

    url = f"{API_ROOT}/repos/{repo}/issues/{number}"
    with httpx.Client(timeout=15.0, transport=transport) as client:  # type: ignore[arg-type]
        resp = client.get(url, headers=github_headers(token))
        resp.raise_for_status()
        body = resp.json().get("body")
    return str(body) if body else None


# Assessments are cheap but each one costs two GitHub calls, and the triage UI
# requests one per issue card on every render — cache per (repo, number) so
# re-renders within the TTL are free.
_CACHE_TTL_S = 600.0
_CACHE_MAX = 1024
_cache: dict[tuple[str, int], tuple[float, ResolutionAssessment]] = {}
_cache_lock = threading.Lock()


def _cache_get(key: tuple[str, int]) -> ResolutionAssessment | None:
    with _cache_lock:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL_S:
            return hit[1]
        if hit:
            del _cache[key]
    return None


def _cache_put(key: tuple[str, int], value: ResolutionAssessment) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            cutoff = time.monotonic() - _CACHE_TTL_S
            for k in [k for k, (ts, _) in _cache.items() if ts < cutoff]:
                del _cache[k]
            if len(_cache) >= _CACHE_MAX:  # everything still fresh — drop oldest
                del _cache[min(_cache, key=lambda k: _cache[k][0])]
        _cache[key] = (time.monotonic(), value)


def assess_issue(
    repo: str,
    number: int,
    *,
    issue_body: str | None = None,
    token: str | None = None,
    use_mocks: bool = False,
    transport: object | None = None,
) -> ResolutionAssessment:
    """End-to-end: fetch comments + issue body, run the detector. Cached.

    Returns a 'none' assessment in mock mode. When ``issue_body`` isn't
    supplied, it is fetched — body-only signals matter for issues left open
    for tracking after a fix.
    """
    if use_mocks or not token:
        log.info("ingest.comments.assess", repo=repo, number=number, mocked=True)
        # In mock mode the issue list is synthetic and not worth a GitHub call.
        return ResolutionAssessment(confidence="none", label="", signals=[], comment_count=0)

    key = (repo, number)
    if cached := _cache_get(key):
        return cached

    comments = fetch_recent_comments(repo, number, token=token, transport=transport)
    if issue_body is None:
        try:
            issue_body = fetch_issue_body(repo, number, token=token, transport=transport)
        except Exception as exc:
            log.warning(
                "ingest.comments.body_fetch_failed", repo=repo, number=number, error=str(exc)
            )
    assessment = detect_resolution(comments, issue_body)
    _cache_put(key, assessment)
    return assessment
