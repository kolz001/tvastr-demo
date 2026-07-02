"""SDK-schema grounding primitives.

When a diagnosis hinges on a third-party response shape, these helpers fetch
the SDK's published wheel (host pip, wheels-only, no-deps, isolated target)
and extract the class definitions relevant to the diagnosis so the grounding
step can validate field names against ground truth instead of web hearsay.

Nothing fetched here is ever imported or executed — snippets are text.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from tvastr.analysis._jsonutil import extract_all_json
from tvastr.logging import get_logger

log = get_logger(__name__)

_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
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
    """Parse + validate the probe response. None ⇔ not relevant or unusable."""
    merged: dict = {}
    for obj in extract_all_json(text):
        merged.update(obj)
    if not merged.get("relevant"):
        return None
    package = str(merged.get("package") or "")
    if not _PACKAGE_RE.match(package):
        log.warning("sdk_schema.probe.bad_package", package=package[:120])
        return None
    raw_keywords = merged.get("keywords") or []
    if not isinstance(raw_keywords, list):
        return None
    keywords = tuple(str(k) for k in raw_keywords if str(k).strip())[:_MAX_KEYWORDS]
    if not keywords:
        return None
    version = merged.get("version_hint")
    version_hint = str(version) if version else None
    return SchemaProbe(package=package, version_hint=version_hint, keywords=keywords)


def fetch_sdk(
    package: str,
    version_hint: str | None,
    cache_root: Path = Path("data/sdk_cache"),
) -> Path | None:
    """Install the package's published wheel into an isolated cache dir.

    Wheels-only (no setup.py execution), no dependencies, list-form argv.
    Returns the target dir, or None on any failure. A failing version pin is
    retried once without the pin.
    """
    if not _PACKAGE_RE.match(package):
        return None
    target = cache_root / f"{package}-{version_hint or 'latest'}"
    if target.is_dir() and any(target.iterdir()):
        return target
    for spec in dict.fromkeys(
        [f"{package}=={version_hint}" if version_hint else package, package]
    ):
        argv = [
            sys.executable, "-m", "pip", "install",
            "--only-binary=:all:", "--no-deps", "--quiet",
            "--target", str(target), spec,
        ]
        try:
            result = subprocess.run(argv, capture_output=True, timeout=_PIP_TIMEOUT_S)
        except Exception as exc:  # timeout, missing pip — degrade, never raise
            log.warning("sdk_schema.fetch.error", package=package, error=str(exc))
            return None
        if result.returncode == 0 and target.is_dir() and any(target.iterdir()):
            return target
        log.warning(
            "sdk_schema.fetch.pip_failed",
            spec=spec,
            stderr=(result.stderr or b"")[-300:].decode(errors="replace"),
        )
    return None


_CLASS_RE = re.compile(r"^class\s+\w+.*:", re.MULTILINE)


def extract_schema_snippets(
    root: Path,
    keywords: Sequence[str],
    max_snippets: int = 6,
    max_lines_each: int = 40,
) -> list[Snippet]:
    """Deterministically pull top-level class blocks containing any keyword."""
    snippets: list[Snippet] = []
    files = sorted(list(root.rglob("*.py")) + list(root.rglob("*.pyi")))
    for f in files:
        if len(snippets) >= max_snippets:
            break
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        starts = [m.start() for m in _CLASS_RE.finditer(text)]
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(text)
            block = text[start:end].rstrip()
            if not any(k in block for k in keywords):
                continue
            lines = block.splitlines()
            if len(lines) > max_lines_each:
                lines = [*lines[:max_lines_each], "    ..."]
            snippets.append(
                Snippet(path=str(f.relative_to(root)), text="\n".join(lines))
            )
            if len(snippets) >= max_snippets:
                break
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
