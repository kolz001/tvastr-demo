"""SDK-schema grounding primitives.

When a diagnosis hinges on a third-party response shape, these helpers fetch
the SDK's published wheel (host pip, wheels-only, no-deps, isolated target)
and extract the class definitions relevant to the diagnosis so the grounding
step can validate field names against ground truth instead of web hearsay.

Nothing fetched here is ever imported or executed — snippets are text.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tvastr.analysis._jsonutil import extract_all_json
from tvastr.logging import get_logger

log = get_logger(__name__)

_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!*-]{0,63}$")
_MAX_KEYWORDS = 4
_PIP_TIMEOUT_S = 120

PROBE_SYSTEM = (
    "You decide whether validating a bug diagnosis requires inspecting a "
    "third-party Python library's type definitions, and if so which pip "
    "package defines them. Reply with ONLY one JSON object, no prose."
)


@dataclass(frozen=True)
class SchemaProbe:
    package: str
    version_hint: str | None
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Snippet:
    path: str
    text: str


def build_probe_prompt(
    title: str, summary: str, suspected_files: list[str], issue_snippet: str
) -> str:
    return (
        f"Failure: {title}\n"
        f"Current diagnosis: {summary}\n"
        f"Suspected repository files: {', '.join(suspected_files) or '(none)'}\n\n"
        f"Issue excerpt:\n{issue_snippet[:1500]}\n\n"
        "Does validating this diagnosis depend on the exact shape of a "
        "third-party (pip-installable) library's objects — response types, "
        "field names, enums? Answer with ONLY this JSON:\n"
        '{"relevant": <bool>, "package": "<pip distribution defining those '
        'types, e.g. google-genai>", "version_hint": "<that package\'s version '
        'if the issue states one, else null>", "keywords": ["2-4 class/field '
        'names to locate, e.g. usage_metadata"]}'
    )


def parse_probe(text: str) -> SchemaProbe | None:
    """Parse + validate the probe response. None ⇔ not relevant or unusable.

    Takes only the FIRST JSON object that carries a "relevant" key, rather
    than merging every object in the response — the issue excerpt is
    attacker-controlled and the model may echo it back verbatim, so a
    trailing object like ``{"package": "evil-pkg"}`` must not be able to
    clobber the model's own probe object field-by-field.
    """
    probe_obj = next((obj for obj in extract_all_json(text) if "relevant" in obj), None)
    if probe_obj is None or not probe_obj.get("relevant"):
        return None
    package = str(probe_obj.get("package") or "")
    if not _PACKAGE_RE.match(package):
        log.warning("sdk_schema.probe.bad_package", package=package[:120])
        return None
    raw_keywords = probe_obj.get("keywords") or []
    if not isinstance(raw_keywords, list):
        return None
    keywords = tuple(str(k) for k in raw_keywords if str(k).strip())[:_MAX_KEYWORDS]
    if not keywords:
        return None
    version = probe_obj.get("version_hint")
    version_hint = _valid_version_hint(str(version)) if version else None
    return SchemaProbe(package=package, version_hint=version_hint, keywords=keywords)


def _valid_version_hint(version_hint: str | None) -> str | None:
    """Return version_hint if it looks like a PEP-440-shaped token, else None.

    version_hint is LLM-controlled and feeds a pip `--target` path; an
    unvalidated value like "/../../../tmp/pwned" could escape cache_root.
    The hint is best-effort advisory, so an invalid hint degrades to None
    (fetch latest) rather than rejecting the whole probe/fetch.
    """
    if version_hint and _VERSION_RE.match(version_hint):
        return version_hint
    return None


def fetch_sdk(
    package: str,
    version_hint: str | None,
    cache_root: Path = Path("data/sdk_cache"),
) -> Path | None:
    """Install the package's published wheel into an isolated cache dir.

    Wheels-only (no setup.py execution), no dependencies, list-form argv.
    Returns the target dir, or None on every attempt failing. A failing
    version pin is retried once without the pin.

    The cache key is computed PER ATTEMPT — a pinned attempt caches under
    ``{package}@{version_hint}``, the unpinned fallback under
    ``{package}@latest`` (the ``@`` separator sits outside both
    _PACKAGE_RE/_VERSION_RE, so it can't collide with a package name that
    itself contains a hyphen, e.g. "google" pinned to "genai-latest" vs
    "google-genai"). This also means a pin that fell back to latest never
    poisons the cache for a later call that pins the same version: that
    later call finds no ``{package}@{version_hint}`` dir and re-runs pip.
    """
    if not _PACKAGE_RE.match(package):
        return None
    version_hint = _valid_version_hint(version_hint)
    attempts: list[str | None] = [version_hint, None] if version_hint else [None]
    for pin in dict.fromkeys(attempts):
        target = cache_root / f"{package}@{pin or 'latest'}"
        if target.is_dir() and any(target.iterdir()):
            return target
        spec = f"{package}=={pin}" if pin else package
        argv = [
            sys.executable, "-m", "pip", "install",
            "--only-binary=:all:", "--no-deps", "--quiet",
            "--target", str(target), spec,
        ]
        try:
            result = subprocess.run(argv, capture_output=True, timeout=_PIP_TIMEOUT_S)
        except Exception as exc:  # timeout, missing pip — degrade, never raise
            log.warning("sdk_schema.fetch.error", package=package, error=str(exc))
            shutil.rmtree(target, ignore_errors=True)
            return None
        if result.returncode == 0 and target.is_dir() and any(target.iterdir()):
            return target
        log.warning(
            "sdk_schema.fetch.pip_failed",
            spec=spec,
            stderr=(result.stderr or b"")[-300:].decode(errors="replace"),
        )
        shutil.rmtree(target, ignore_errors=True)
    return None


_CLASS_RE = re.compile(r"^class\s+\w+", re.MULTILINE)
_SKIP_DIR_PARTS = {"tests", "test", "testing"}


def extract_schema_snippets(
    root: Path,
    keywords: Sequence[str],
    max_snippets: int = 6,
    max_lines_each: int = 40,
) -> list[Snippet]:
    """Deterministically pull top-level class blocks containing any keyword.

    Wheels ship test suites that can eat snippet slots, so files under a
    tests/test/testing dir component are skipped entirely. The snippet
    budget is allocated round-robin PER KEYWORD (rather than first-N over
    the whole file walk) so a common keyword with many matches can't starve
    a rare keyword whose class is the actual evidence. A block is dropped
    if, after truncation to max_lines_each, none of the keywords remain in
    the emitted text — a match deep inside a huge class is useless once the
    truncation window cuts it off.
    """
    files = sorted(list(root.rglob("*.py")) + list(root.rglob("*.pyi")))
    # One bucket per keyword, in keyword order; a block that matches several
    # keywords is assigned to the first keyword (in `keywords` order) it
    # matches, so it is never counted twice.
    buckets: dict[str, list[Snippet]] = {k: [] for k in keywords}
    seen_blocks: set[tuple[str, int]] = set()

    for f in files:
        try:
            rel = f.relative_to(root)
        except ValueError:
            continue
        if _SKIP_DIR_PARTS & set(rel.parts[:-1]):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        starts = [m.start() for m in _CLASS_RE.finditer(text)]
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(text)
            block = text[start:end].rstrip()
            keyword = next((k for k in keywords if k in block), None)
            if keyword is None or (str(rel), start) in seen_blocks:
                continue
            seen_blocks.add((str(rel), start))
            lines = block.splitlines()
            if len(lines) > max_lines_each:
                lines = [*lines[:max_lines_each], "    ..."]
            snippet_text = "\n".join(lines)
            if not any(k in snippet_text for k in keywords):
                continue  # keyword match didn't survive truncation
            buckets[keyword].append(Snippet(path=str(rel), text=snippet_text))

    snippets: list[Snippet] = []
    round_idx = 0
    while len(snippets) < max_snippets and any(
        round_idx < len(buckets[k]) for k in keywords
    ):
        for k in keywords:
            if round_idx < len(buckets[k]):
                snippets.append(buckets[k][round_idx])
                if len(snippets) >= max_snippets:
                    break
        round_idx += 1
    return snippets


def format_schema_block(package: str, snippets: list[Snippet]) -> str:
    if not snippets:
        return ""
    parts = [
        f"Authoritative type definitions from the installed `{package}` SDK "
        "(ground truth for field names):"
    ]
    for s in snippets:
        parts.append(f"# {s.path}\n{s.text}")
    return "\n\n".join(parts)
