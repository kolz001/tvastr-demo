"""Weekly consolidation + ranking: 7 daily digests -> one ranked top-N report.

``consolidate_week`` merges a week's :class:`~tvastr.selfheal.scan.DailyDigest`
clusters by fingerprint (summing counts, widening the first/last-seen range),
scores each merged cluster ``count * severity_weight``, and keeps the top
``top_n`` — with the top ``fix_n`` marked as ``to_fix`` candidates and the rest
as ``report_only``. Task 5 consumes the resulting :class:`WeeklyReport` to
decide what to actually attempt fixing.

``SEVERITY_WEIGHTS`` is the single tuning surface for how much a cluster's
*kind* matters relative to its raw frequency: a rare quality signal (the agent
under-confident, or a fix that broke on rerun) should usually outrank a
frequent but mundane ops error, and a cluster that's just a known/handled
upstream hiccup (GitHub rate limits mapped to an explained 502) should sink
toward the bottom regardless of how often it recurs.

**The ``kind`` discriminator.** ``FailurePattern`` (``domain/models.py``) has
no ``attributes``/``origin`` field, and the per-event ``origin``
("selflog"/"runs") that ``scan.py`` stamps onto each candidate ``LogEvent``
does not survive into the persisted cluster JSONL at all — only
``sample_messages`` does (see the additive fix in ``scan.py``'s
``DailyDigest.sample_messages``). So ``origin`` can't discriminate ops vs.
quality; even if it could, "runs" alone wouldn't, since scan.py emits both ops
*and* quality candidates with ``origin="runs"``.

What *does* survive reliably is the message text itself: scan.py's two
quality-signal generators (``_confidence_zero_log_event``,
``_repro_broken_log_event``) emit fixed, non-interpolated literals —
``"investigator returned confidence 0.0"`` and ``"verify verdict
repro_broken"`` — never formatted with variable data. Every other candidate
generator (ops errors, llm.call failures, selflog records) interpolates
run-specific detail into its message. That asymmetry makes literal matching
against ``representative_message`` a fully reliable discriminator without
touching ``scan.py``'s schema: a quality cluster's representative message is
*always* one of the two fixed strings, verbatim.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from tvastr.selfheal.scan import load_daily

# The single tuning surface for cluster ranking. "quality" (agent under-
# confident, or a verified fix broke on rerun) outranks "ops" (mundane
# pipeline/infra errors) at equal frequency; "handled_upstream" (a known,
# already-explained upstream failure -- e.g. a GitHub rate limit mapped to a
# 502) is dampened well below both.
SEVERITY_WEIGHTS: dict[str, float] = {
    "quality": 3.0,
    "ops": 1.0,
    "handled_upstream": 0.25,
}

# Fixed literals scan.py's quality-signal generators emit verbatim -- see the
# module docstring for why this is reliable without any scan.py schema change.
_QUALITY_MESSAGE_MARKERS: tuple[str, ...] = (
    "investigator returned confidence 0.0",
    "verify verdict repro_broken",
)

# A cluster whose representative message matches any of these is a known,
# already-explained upstream failure -- dampened to "handled_upstream"
# regardless of its ops/quality kind. Mirrors the mapped-502 wording in
# api/routes/issues.py (`f"GitHub issue search failed ({status...}){hint}"`)
# plus generic rate-limit hints.
_HANDLED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"GitHub .* failed \(\d+"),
    re.compile(r"rate[ -]?limit", re.IGNORECASE),
)

_MAX_SAMPLE_MESSAGES = 3


@dataclass(frozen=True)
class RankedCluster:
    """One merged, scored failure cluster in a :class:`WeeklyReport`."""

    fingerprint: str
    title: str
    count: int
    severity_weight: float
    score: float
    kind: str
    sample_messages: list[str]
    first_seen: str
    last_seen: str


@dataclass(frozen=True)
class WeeklyReport:
    """A week's consolidated, ranked, top-N failure report."""

    week: str
    generated_at: str
    to_fix: list[RankedCluster]
    report_only: list[RankedCluster]
    days_scanned: list[str]


def week_key(day: str) -> str:
    """ISO week key for a ``YYYY-MM-DD`` day string, e.g. ``"2026-W34"``."""
    iso = date.fromisoformat(day).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _week_days(week: str) -> list[str]:
    """Monday..Sunday ``YYYY-MM-DD`` day strings for an ISO week key."""
    year_str, week_str = week.split("-W")
    year, week_num = int(year_str), int(week_str)
    return [date.fromisocalendar(year, week_num, weekday).isoformat() for weekday in range(1, 8)]


def _cluster_kind(representative_message: str) -> str:
    if any(marker in representative_message for marker in _QUALITY_MESSAGE_MARKERS):
        return "quality"
    return "ops"


def _is_handled_upstream(representative_message: str) -> bool:
    return any(pattern.search(representative_message) for pattern in _HANDLED_PATTERNS)


@dataclass
class _Merged:
    """Mutable accumulator for one fingerprint's cross-day merge, before scoring."""

    title: str
    representative_message: str
    count: int
    first_seen: str
    last_seen: str
    sample_messages: list[str] = field(default_factory=list)

    def absorb(self, *, count: int, first_seen: str, last_seen: str, samples: list[str]) -> None:
        self.count += count
        self.first_seen = min(self.first_seen, first_seen)
        self.last_seen = max(self.last_seen, last_seen)
        for sample in samples:
            if len(self.sample_messages) >= _MAX_SAMPLE_MESSAGES:
                break
            if sample not in self.sample_messages:
                self.sample_messages.append(sample)


def _rank(merged: dict[str, _Merged]) -> list[RankedCluster]:
    ranked: list[RankedCluster] = []
    for fingerprint, entry in merged.items():
        kind = _cluster_kind(entry.representative_message)
        handled = _is_handled_upstream(entry.representative_message)
        weight = SEVERITY_WEIGHTS["handled_upstream" if handled else kind]
        ranked.append(
            RankedCluster(
                fingerprint=fingerprint,
                title=entry.title,
                count=entry.count,
                severity_weight=weight,
                score=entry.count * weight,
                kind=kind,
                sample_messages=entry.sample_messages,
                first_seen=entry.first_seen,
                last_seen=entry.last_seen,
            )
        )
    ranked.sort(key=lambda c: (c.score, c.count, c.fingerprint), reverse=True)
    return ranked


def _weekly_path(out_dir: Path, week: str) -> Path:
    return out_dir / "weekly" / f"{week}.json"


def _write_report(report: WeeklyReport, out_dir: Path) -> None:
    path = _weekly_path(out_dir, report.week)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(report), indent=2), encoding="utf-8")


def consolidate_week(
    week: str,
    *,
    digests_dir: Path,
    out_dir: Path,
    top_n: int = 10,
    fix_n: int = 3,
) -> WeeklyReport:
    """Merge one ISO week's daily digests into a ranked top-``top_n`` report.

    Missing days (no digest ever scanned) are skipped silently -- only days a
    digest actually exists for are recorded in ``days_scanned``. Always writes
    ``out_dir/weekly/{week}.json``, even for an empty week (same proof-of-life
    convention as ``scan.py``'s daily digest).
    """
    days_scanned: list[str] = []
    merged: dict[str, _Merged] = {}

    for day in _week_days(week):
        digest = load_daily(digests_dir, day)
        if digest is None:
            continue
        days_scanned.append(day)
        for cluster in digest.clusters:
            samples = digest.sample_messages.get(cluster.fingerprint, [])
            existing = merged.get(cluster.fingerprint)
            if existing is None:
                merged[cluster.fingerprint] = _Merged(
                    title=cluster.title,
                    representative_message=cluster.representative_message,
                    count=cluster.count,
                    first_seen=cluster.first_seen.isoformat(),
                    last_seen=cluster.last_seen.isoformat(),
                    sample_messages=list(samples[:_MAX_SAMPLE_MESSAGES]),
                )
            else:
                existing.absorb(
                    count=cluster.count,
                    first_seen=cluster.first_seen.isoformat(),
                    last_seen=cluster.last_seen.isoformat(),
                    samples=samples,
                )

    ranked = _rank(merged)[:top_n]
    report = WeeklyReport(
        week=week,
        generated_at=datetime.now(UTC).isoformat(),
        to_fix=ranked[:fix_n],
        report_only=ranked[fix_n:],
        days_scanned=days_scanned,
    )
    _write_report(report, out_dir)
    return report


def load_latest_report(out_dir: Path) -> WeeklyReport | None:
    """Load the lexically-latest ``out_dir/weekly/*.json`` report.

    ISO week keys (``YYYY-Www``) sort lexically in chronological order, so the
    max filename is always the most recent week. ``None`` if no weekly report
    has ever been written.
    """
    weekly_dir = out_dir / "weekly"
    if not weekly_dir.exists():
        return None
    paths = sorted(weekly_dir.glob("*.json"))
    if not paths:
        return None

    payload = json.loads(paths[-1].read_text(encoding="utf-8"))
    return WeeklyReport(
        week=payload["week"],
        generated_at=payload["generated_at"],
        to_fix=[RankedCluster(**row) for row in payload["to_fix"]],
        report_only=[RankedCluster(**row) for row in payload["report_only"]],
        days_scanned=payload["days_scanned"],
    )
