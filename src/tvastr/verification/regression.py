"""Scoped regression checks: which existing tests touch the files we changed?

For non-crashing fixes we can't always show "the reproducer stopped raising",
so we run the slice of the project's tests that import any of the changed
modules. Best-effort discovery — we look for ``tests/test_<basename>.py``,
``test_<basename>.py``, or imports of the changed module path in test files.
"""

from __future__ import annotations

import re
from pathlib import Path

from tvastr.domain import FileChange
from tvastr.logging import get_logger
from tvastr.verification.models import RunResult
from tvastr.verification.sandbox import SandboxHandle

log = get_logger(__name__)


def _module_from_path(path: str) -> str:
    """``llama_index/llms/openai/base.py`` → ``llama_index.llms.openai.base``."""
    p = Path(path)
    if p.suffix == ".py":
        p = p.with_suffix("")
    return ".".join(p.parts)


def discover_scoped_tests(changes: list[FileChange], project_root: Path) -> list[str]:
    """Return paths of test files that look like they exercise ``changes``."""
    if not project_root.exists():
        return []

    targets: set[str] = set()
    for change in changes:
        basename = Path(change.path).stem
        targets.add(basename)
        targets.add(_module_from_path(change.path))

    discovered: list[str] = []
    for test_file in project_root.rglob("test_*.py"):
        if "/.venv/" in str(test_file) or "/site-packages/" in str(test_file):
            continue
        text = test_file.read_text(encoding="utf-8", errors="ignore")
        # Heuristic 1: filename mention (``tests/test_<basename>.py``)
        if any(t in test_file.stem for t in targets):
            discovered.append(str(test_file.relative_to(project_root)))
            continue
        # Heuristic 2: import of the module path
        for target in targets:
            if re.search(rf"\b(from|import)\s+{re.escape(target)}\b", text):
                discovered.append(str(test_file.relative_to(project_root)))
                break

    # Stable order, no dupes
    return sorted(set(discovered))


def run_scoped_tests(
    handle: SandboxHandle, test_paths: list[str], *, timeout_s: int = 120
) -> tuple[RunResult, dict[str, int]]:
    """Run the scoped tests via pytest in the sandbox.

    Returns ``(run_result, counts)`` where counts has keys ``passed``,
    ``failed``, ``errors``, ``skipped``. Counts are parsed from pytest's tail
    line on a best-effort basis; -1 indicates "could not parse".
    """
    cmd = ["python", "-m", "pytest", "-q", "--tb=short", *test_paths]
    result = handle.run(cmd, timeout_s=timeout_s)
    counts = _parse_pytest_summary(result.stdout + "\n" + result.stderr)
    log.info(
        "verify.regression.done",
        tests=len(test_paths),
        exit_code=result.exit_code,
        **counts,
    )
    return result, counts


_PYTEST_SUMMARY_RE = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|deselected|xfailed|xpassed)",
    re.IGNORECASE,
)


def _parse_pytest_summary(output: str) -> dict[str, int]:
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for match in _PYTEST_SUMMARY_RE.finditer(output):
        n = int(match.group(1))
        kind = match.group(2).lower().rstrip("s")  # "errors" → "error"
        if kind == "error":
            counts["errors"] += n
        elif kind in counts:
            counts[kind] += n
    return counts
