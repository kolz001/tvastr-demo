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

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from tvastr.config import Settings
from tvastr.domain import FileChange
from tvastr.logging import get_logger
from tvastr.verification.models import ProvisionResult, RunResult

log = get_logger(__name__)

_BOOTSTRAP_SRC = '''\
import importlib.util, json, pathlib, shutil
mani = json.loads(pathlib.Path("/work/.tvastr_patch/manifest.json").read_text())
for module, staged in mani:
    spec = importlib.util.find_spec(module)
    if not (spec and spec.origin):
        print(f"[tvastr] skip unresolved module: {module}")
        continue
    try:
        shutil.copyfile(f"/work/.tvastr_patch/{staged}", spec.origin)
        print(f"[tvastr] patched {module} -> {spec.origin}")
    except Exception as e:
        print(f"[tvastr] skip uncopyable {module}: {e}")
'''


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


def distribution_for_path(repo_path: str) -> str | None:
    """Pip distribution name for a repo-relative source path, or ``None``.

    llama_index is a monorepo: each integration is a separately-installable
    distribution whose dir is hyphenated (``llama-index-vector-stores-s3``) and
    sits immediately above the ``llama_index`` import root. Return that dir name
    when it starts with ``llama-index-``; otherwise ``None`` (flat checkout,
    notebook, or a path with no integration dir).
    """
    parts = [p for p in repo_path.split("/") if p]
    try:
        idx = parts.index("llama_index")
    except ValueError:
        return None
    if idx == 0:
        return None
    candidate = parts[idx - 1]
    return candidate if candidate.startswith("llama-index-") else None


@runtime_checkable
class SandboxHandle(Protocol):
    """A live sandbox the verifier owns for the duration of one verification."""

    root: Path  # the workspace path on the host

    def write_file(self, relpath: str, content: str) -> None: ...
    def apply_changes(self, changes: list[FileChange]) -> None: ...
    def provision(self, dists: list[str]) -> ProvisionResult: ...
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
        self._deps_dir: Path | None = None

    def write_file(self, relpath: str, content: str) -> None:
        full = self.root / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    def apply_changes(self, changes: list[FileChange]) -> None:
        for change in changes:
            self.write_file(change.path, change.patched_content)

    def _run_env(self) -> dict[str, str] | None:
        if self._deps_dir is None:
            return None
        env = dict(os.environ)
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{self._deps_dir}{os.pathsep}{prev}" if prev else str(self._deps_dir)
        )
        return env

    def provision(self, dists: list[str]) -> ProvisionResult:
        dists = [d for d in dict.fromkeys(dists) if d]  # dedup, drop empties
        if not dists:
            return ProvisionResult(requested=[], installed=[], failed=[], ok=True)
        deps_dir = self.root / ".tvastr_deps"
        cmd = [sys.executable, "-m", "pip", "install", "--target", str(deps_dir), *dists]
        log.info("verify.sandbox.provision", sandbox="subprocess", dists=dists)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
        except Exception as exc:  # network down, pip missing, timeout
            log.warning("verify.sandbox.provision.error", error=str(exc))
            return ProvisionResult(requested=dists, installed=[], failed=dists, ok=False)
        if proc.returncode == 0:
            self._deps_dir = deps_dir
            return ProvisionResult(requested=dists, installed=dists, failed=[], ok=True)
        log.warning("verify.sandbox.provision.failed", stderr=proc.stderr[-400:])
        if deps_dir.exists():
            self._deps_dir = deps_dir  # expose whatever landed
        # conservative: a partial multi-package failure can't be attributed,
        # so report none installed though some may have landed.
        return ProvisionResult(requested=dists, installed=[], failed=dists, ok=False)

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
                env=self._run_env(),
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
        self._patch_pending = False
        self._deps_provisioned = False

    def write_file(self, relpath: str, content: str) -> None:
        full = self.root / relpath
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    def apply_changes(self, changes: list[FileChange]) -> None:
        manifest: list[list[str]] = []
        for change in changes:
            module = installed_module_path(change.path)
            if module is None:
                # Not an importable package file (notebook, top-level script):
                # write the repo path as before — it won't be imported, but we
                # don't lose the content.
                self.write_file(change.path, change.patched_content)
                log.info("verify.sandbox.patch.no_module", path=change.path)
                continue
            staged = f"{module}.py"
            self.write_file(f".tvastr_patch/{staged}", change.patched_content)
            manifest.append([module, staged])
        if manifest:
            self.write_file(".tvastr_patch/manifest.json", json.dumps(manifest))
            self.write_file(".tvastr_apply.py", _BOOTSTRAP_SRC)
            self._patch_pending = True
            log.info("verify.sandbox.patch.staged", modules=[m for m, _ in manifest])

    def provision(self, dists: list[str]) -> ProvisionResult:
        dists = [d for d in dict.fromkeys(dists) if d]
        if not dists:
            return ProvisionResult(requested=[], installed=[], failed=[], ok=True)
        # No --tmpfs here (unlike the hardened run): this prep run already drops
        # --read-only, so /tmp lives on the container's writable (disk-backed)
        # overlay with ample space. A RAM-backed tmpfs overflows on the full dep
        # tree pip unpacks under HOME=/tmp (numpy/pillow/sqlalchemy/core/...).
        docker_cmd = [
            "docker", "run", "--rm",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--cap-drop=ALL",
            "-e", "PIP_NO_CACHE_DIR=1",
            "-e", "HOME=/tmp",
            "-v", f"{self.root}:/work:rw",
            "-w", "/work",
            self.image,
            "pip", "install", "--target", "/work/.tvastr_deps", *dists,
        ]
        log.info("verify.sandbox.provision", sandbox="docker", dists=dists, image=self.image)
        try:
            proc = subprocess.run(
                docker_cmd, capture_output=True, text=True, timeout=300, check=False
            )
        except Exception as exc:
            log.warning("verify.sandbox.provision.error", error=str(exc))
            return ProvisionResult(requested=dists, installed=[], failed=dists, ok=False)
        if proc.returncode == 0:
            self._deps_provisioned = True
            return ProvisionResult(requested=dists, installed=dists, failed=[], ok=True)
        log.warning("verify.sandbox.provision.failed", stderr=proc.stderr[-400:])
        if (self.root / ".tvastr_deps").exists():
            self._deps_provisioned = True
        # conservative: a partial multi-package failure can't be attributed,
        # so report none installed though some may have landed.
        return ProvisionResult(requested=dists, installed=[], failed=dists, ok=False)

    def run(self, cmd: list[str], *, timeout_s: int = 60) -> RunResult:
        if self._patch_pending:
            # Apply staged patches onto the installed modules inside THIS
            # container, then run the reproducer — one container so the overwrite
            # persists for the import. site-packages must be writable for the
            # copy, so --read-only is dropped for this run ONLY; all other
            # hardening (no network, no caps, --rm, tmpfs) is kept.
            readonly: list[str] = []
            inner = "python /work/.tvastr_apply.py && " + shlex.join(cmd)
        else:
            readonly = ["--read-only"]
            inner = shlex.join(cmd)
        if self._deps_provisioned:
            # Prepend (don't clobber any image PYTHONPATH) so the provisioned
            # integration wins and the patch-applier's find_spec can resolve it.
            # Use ${PYTHONPATH:+:$PYTHONPATH} to avoid a trailing colon when the
            # image sets no PYTHONPATH (a trailing empty entry = /work on sys.path).
            inner = "export PYTHONPATH=/work/.tvastr_deps${PYTHONPATH:+:$PYTHONPATH} && " + inner
            run_cmd = ["sh", "-c", inner]
        elif self._patch_pending:
            run_cmd = ["sh", "-c", inner]
        else:
            run_cmd = list(cmd)
        docker_cmd = [
            "docker",
            "run",
            "--rm",
            *readonly,
            "--network=none",
            "--cap-drop=ALL",
            "--tmpfs=/tmp:rw,size=64m",
            "-v",
            f"{self.root}:/work:rw",
            "-w",
            "/work",
            self.image,
            *run_cmd,
        ]
        log.info("verify.sandbox.run", sandbox="docker", cmd=cmd, image=self.image,
                 patched=self._patch_pending)
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
