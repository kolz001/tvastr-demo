"""Hermetic sandboxes for running reproducers + applying patches.

Two implementations, same ``Sandbox`` Protocol:

- :class:`DockerSandbox` — strong isolation. Uses ``docker run`` against the
  pre-built ``Dockerfile.llamaindex`` image with ``--read-only --network=none
  --cap-drop=ALL`` so synthesized Python in the reproducer can't phone home
  or write outside ``/work``. Preferred when ``docker`` is on PATH.
- :class:`SubprocessSandbox` — fallback. Creates a temp directory; ``run``
  executes commands with the host Python in that directory. No venv setup
  per run (slow); intended for environments without Docker.

The factory :func:`build_sandbox` picks Docker when available, subprocess
otherwise, and lets settings override.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from tvastr.config import Settings
from tvastr.domain import FileChange
from tvastr.logging import get_logger
from tvastr.verification.models import RunResult

log = get_logger(__name__)


def installed_module_path(repo_path: str) -> str | None:
    """Dotted module for a repo-relative source path, or ``None`` if not importable.

    Distribution dirs are hyphenated (``llama-index-core``); import packages are
    underscored (``llama_index``). Drop everything up to and including the LAST
    non-identifier segment, then dot-join the remaining identifier segments
    (``.py`` stripped). Notebooks, non-.py files, and bare single-segment paths
    return ``None``.
    """
    parts = [p for p in repo_path.split("/") if p]
    if len(parts) < 2 or not parts[-1].endswith(".py"):
        return None
    norm = [*parts[:-1], parts[-1][:-3]]  # strip .py on the file segment
    start = 0
    for i, seg in enumerate(norm):
        if not seg.isidentifier():
            start = i + 1
    mods = norm[start:]
    if not mods or any(not s.isidentifier() for s in mods):
        return None
    return ".".join(mods)


@runtime_checkable
class SandboxHandle(Protocol):
    """A live sandbox the verifier owns for the duration of one verification."""

    root: Path  # the workspace path on the host

    def write_file(self, relpath: str, content: str) -> None: ...
    def apply_changes(self, changes: list[FileChange]) -> None: ...
    def run(self, cmd: list[str], *, timeout_s: int = 60) -> RunResult: ...
    def discard(self) -> None: ...


@runtime_checkable
class Sandbox(Protocol):
    name: str

    def prepare(self) -> SandboxHandle: ...


# ─── Subprocess (fallback) ────────────────────────────────────────────────


class _SubprocessHandle:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write_file(self, relpath: str, content: str) -> None:
        full = self.root / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    def apply_changes(self, changes: list[FileChange]) -> None:
        for change in changes:
            self.write_file(change.path, change.patched_content)

    def run(self, cmd: list[str], *, timeout_s: int = 60) -> RunResult:
        log.info("verify.sandbox.run", sandbox="subprocess", cmd=cmd, cwd=str(self.root))
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            return RunResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                exit_code=-1,
                stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                stderr=exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
                timed_out=True,
            )
        except FileNotFoundError as exc:
            return RunResult(exit_code=127, stdout="", stderr=str(exc))

    def discard(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class SubprocessSandbox:
    name = "subprocess"

    def prepare(self) -> _SubprocessHandle:
        root = Path(tempfile.mkdtemp(prefix="tvastr-verify-"))
        log.info("verify.sandbox.prepare", sandbox=self.name, root=str(root))
        return _SubprocessHandle(root)


# ─── Docker (preferred) ───────────────────────────────────────────────────


class _DockerHandle:
    """Runs commands inside a one-shot container against a mounted ``/work``.

    Each ``run`` call spawns a fresh container so test runs can't leak state
    between calls — matching the threat model where the reproducer is
    synthesized code we don't trust.
    """

    def __init__(self, root: Path, image: str) -> None:
        self.root = root
        self.image = image

    def write_file(self, relpath: str, content: str) -> None:
        full = self.root / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    def apply_changes(self, changes: list[FileChange]) -> None:
        for change in changes:
            self.write_file(change.path, change.patched_content)

    def run(self, cmd: list[str], *, timeout_s: int = 60) -> RunResult:
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            "--read-only",
            "--network=none",
            "--cap-drop=ALL",
            "--tmpfs=/tmp:rw,size=64m",
            "-v",
            f"{self.root}:/work:rw",
            "-w",
            "/work",
            self.image,
            *cmd,
        ]
        log.info("verify.sandbox.run", sandbox="docker", cmd=cmd, image=self.image)
        try:
            proc = subprocess.run(
                docker_cmd, capture_output=True, text=True, timeout=timeout_s, check=False
            )
            return RunResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)
        except subprocess.TimeoutExpired as exc:
            return RunResult(
                exit_code=-1,
                stdout=exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                stderr=exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
                timed_out=True,
            )

    def discard(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class DockerSandbox:
    name = "docker"

    def __init__(self, image: str = "tvastr-verify:llamaindex") -> None:
        self.image = image

    def prepare(self) -> _DockerHandle:
        root = Path(tempfile.mkdtemp(prefix="tvastr-verify-"))
        log.info("verify.sandbox.prepare", sandbox=self.name, root=str(root), image=self.image)
        return _DockerHandle(root, self.image)


# ─── Factory ──────────────────────────────────────────────────────────────


def build_sandbox(settings: Settings) -> Sandbox:
    """Pick the strongest available sandbox.

    Honours ``TVASTR_VERIFY_SANDBOX`` (`docker` / `subprocess`) when set;
    otherwise auto-detects Docker by looking for it on PATH.
    """
    forced = settings.verify_sandbox
    if forced == "subprocess":
        return SubprocessSandbox()
    if forced == "docker":
        return DockerSandbox(image=settings.verify_docker_image)
    if shutil.which("docker"):
        return DockerSandbox(image=settings.verify_docker_image)
    log.info("verify.sandbox.fallback", reason="docker not on PATH")
    return SubprocessSandbox()
