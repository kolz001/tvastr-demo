# Verify-loop Patch-on-Import-Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Docker verify sandbox apply the agent's patch to the *installed* module the reproducer imports (not the unimported repo path), so verification runs against patched code.

**Architecture:** A pure helper derives the dotted module from the repo path. `_DockerHandle.apply_changes` stages patched files + a manifest + a bootstrap into `/work` and sets `_patch_pending`; the patched rerun runs `sh -c "python /work/.tvastr_apply.py && <cmd>"` in one container (so `find_spec(module).origin` is overwritten before the reproducer imports it) and drops `--read-only` for that run only. Subprocess sandbox is untouched.

**Tech Stack:** Python 3.12, `importlib.util.find_spec`, `shlex`, the Docker verify sandbox (`sandbox.py`) and its `subprocess.run`-mockable test seam.

## Global Constraints

- Module derivation rule: drop path segments up to and including the **last** non-identifier (hyphenated distribution) segment; the remaining segments (`.py` stripped on the file) must all be valid identifiers; dot-join. Bare single-segment paths and non-`.py` paths → `None`.
- Docker-only. `_SubprocessHandle` is NOT modified (best-effort; never mutates the host venv).
- Patched rerun (when `_patch_pending`): drop `--read-only`, KEEP `--network=none`, `--cap-drop=ALL`, `--rm`, `--tmpfs=/tmp:rw,size=64m`. Baseline (before apply) keeps `--read-only`.
- Staging layout: patched files → `/work/.tvastr_patch/<dotted>.py`; manifest → `/work/.tvastr_patch/manifest.json` (a JSON list of `[module, "<dotted>.py"]`); bootstrap → `/work/.tvastr_apply.py`.
- Bootstrap is never fatal: an unresolved/uncopyable module is skipped + logged; the reproducer always runs.
- Files with no derivable module fall back to writing `change.path` (today's behavior), logged.
- MANDATORY before each commit: `uv run pytest -q` green AND `uv run ruff check src tests` prints "All checks passed!".
- Commit footer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `src/tvastr/verification/sandbox.py` (modify) | `installed_module_path()` helper; `_BOOTSTRAP_SRC`; `_DockerHandle` staging + bootstrap + run-wrap + read-only relaxation |
| `tests/test_sandbox.py` (create) | helper units + Docker apply/run tests (mock `subprocess.run`) |

---

## Task 1: `installed_module_path` helper

**Files:**
- Modify: `src/tvastr/verification/sandbox.py`
- Test: `tests/test_sandbox.py` (create)

**Interfaces:**
- Produces: `installed_module_path(repo_path: str) -> str | None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sandbox.py`:

```python
from tvastr.verification.sandbox import installed_module_path


def test_module_path_core():
    assert (
        installed_module_path("llama-index-core/llama_index/core/memory/vector_memory.py")
        == "llama_index.core.memory.vector_memory"
    )


def test_module_path_nested_integration():
    # The real failing case: `llms` is a valid identifier appearing BEFORE the
    # import root `llama_index`, so we must start after the LAST non-identifier dir.
    p = ("llama-index-integrations/llms/llama-index-llms-google-genai/"
         "llama_index/llms/google_genai/utils.py")
    assert installed_module_path(p) == "llama_index.llms.google_genai.utils"


def test_module_path_notebook_is_none():
    assert installed_module_path("docs/examples/x.ipynb") is None


def test_module_path_non_py_is_none():
    assert installed_module_path("llama-index-integrations/llms/x/README.md") is None


def test_module_path_bare_script_is_none():
    assert installed_module_path("script.py") is None


def test_module_path_plain_package():
    assert installed_module_path("pkg/sub/mod.py") == "pkg.sub.mod"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: FAIL — `ImportError: cannot import name 'installed_module_path'`.

- [ ] **Step 3: Implement the helper**

In `src/tvastr/verification/sandbox.py`, add after the imports / `log = get_logger(__name__)` line:

```python
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
    norm = parts[:-1] + [parts[-1][:-3]]  # strip .py on the file segment
    start = 0
    for i, seg in enumerate(norm):
        if not seg.isidentifier():
            start = i + 1
    mods = norm[start:]
    if not mods or any(not s.isidentifier() for s in mods):
        return None
    return ".".join(mods)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Regression + lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all green, lint clean.

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/verification/sandbox.py tests/test_sandbox.py
git commit -m "feat(verify): installed_module_path helper (repo path -> dotted module)"
```

---

## Task 2: Docker staging + bootstrap + run-wrap + read-only relaxation

**Files:**
- Modify: `src/tvastr/verification/sandbox.py`
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Consumes: `installed_module_path` (Task 1).
- Produces: `_DockerHandle.apply_changes` stages importable patches + manifest + bootstrap and sets `_patch_pending`; `_DockerHandle.run` wraps the command with the bootstrap and drops `--read-only` when `_patch_pending`; module-level `_BOOTSTRAP_SRC`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sandbox.py`:

```python
import json
from pathlib import Path

from tvastr.domain import FileChange
from tvastr.verification.sandbox import _DockerHandle


def _docker_handle(tmp_path: Path) -> _DockerHandle:
    return _DockerHandle(tmp_path, "tvastr-verify:img")


def _change(path: str) -> FileChange:
    return FileChange(path=path, patched_content="# patched\n", rationale="r")


class _FakeProc:
    def __init__(self):
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


def _capture_docker(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc()

    monkeypatch.setattr("tvastr.verification.sandbox.subprocess.run", fake_run)
    return calls


def test_baseline_run_is_readonly_and_unwrapped(tmp_path, monkeypatch):
    calls = _capture_docker(monkeypatch)
    h = _docker_handle(tmp_path)
    h.run(["python", "repro.py"])  # before any apply_changes
    cmd = calls[0]
    assert "--read-only" in cmd
    assert "sh" not in cmd  # not wrapped
    assert cmd[-2:] == ["python", "repro.py"]


def test_apply_changes_stages_importable_file(tmp_path):
    h = _docker_handle(tmp_path)
    h.apply_changes([_change("llama-index-core/llama_index/core/memory/vector_memory.py")])
    staged = tmp_path / ".tvastr_patch" / "llama_index.core.memory.vector_memory.py"
    manifest = tmp_path / ".tvastr_patch" / "manifest.json"
    bootstrap = tmp_path / ".tvastr_apply.py"
    assert staged.read_text() == "# patched\n"
    assert bootstrap.exists()
    assert json.loads(manifest.read_text()) == [
        ["llama_index.core.memory.vector_memory", "llama_index.core.memory.vector_memory.py"]
    ]
    assert h._patch_pending is True


def test_apply_changes_nonimportable_writes_repo_path(tmp_path):
    h = _docker_handle(tmp_path)
    h.apply_changes([_change("docs/examples/x.ipynb")])
    assert (tmp_path / "docs/examples/x.ipynb").read_text() == "# patched\n"
    assert not (tmp_path / ".tvastr_patch").exists()
    assert h._patch_pending is False


def test_patched_rerun_is_wrapped_and_not_readonly(tmp_path, monkeypatch):
    calls = _capture_docker(monkeypatch)
    h = _docker_handle(tmp_path)
    h.apply_changes([_change("llama-index-core/llama_index/core/memory/vector_memory.py")])
    h.run(["python", "repro.py"])
    cmd = calls[0]
    assert "--read-only" not in cmd          # relaxed for the patched run
    assert "--network=none" in cmd           # other hardening kept
    assert "--cap-drop=ALL" in cmd
    assert "--rm" in cmd
    joined = " ".join(cmd)
    assert "/work/.tvastr_apply.py && python repro.py" in joined
    assert cmd[cmd.index(h.image) + 1] == "sh"  # entrypoint is sh -c after the image


def test_bootstrap_resolves_and_copies(tmp_path):
    h = _docker_handle(tmp_path)
    h.apply_changes([_change("llama-index-core/llama_index/core/memory/vector_memory.py")])
    src = (tmp_path / ".tvastr_apply.py").read_text()
    assert "find_spec" in src
    assert "shutil.copyfile" in src
    assert ".origin" in src
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sandbox.py -k "rerun or stages or nonimportable or baseline or bootstrap_resolves" -v`
Expected: FAIL — `_DockerHandle` has no `_patch_pending`; `apply_changes` doesn't stage; `run` isn't wrapped.

- [ ] **Step 3: Add `shlex` import and the bootstrap constant**

In `src/tvastr/verification/sandbox.py`, add `import shlex` and `import json` to the imports at the top (alongside `import shutil`, `import subprocess`, `import tempfile`). Then add the bootstrap constant near the other module-level definitions (after `log = get_logger(__name__)` / the helper):

```python
_BOOTSTRAP_SRC = '''\
import importlib.util, json, pathlib, shutil
mani = json.loads(pathlib.Path("/work/.tvastr_patch/manifest.json").read_text())
for module, staged in mani:
    spec = importlib.util.find_spec(module)
    if spec and spec.origin:
        shutil.copyfile(f"/work/.tvastr_patch/{staged}", spec.origin)
        print(f"[tvastr] patched {module} -> {spec.origin}")
    else:
        print(f"[tvastr] skip unresolved module: {module}")
'''
```

- [ ] **Step 4: Add `_patch_pending` to `_DockerHandle.__init__`**

```python
    def __init__(self, root: Path, image: str) -> None:
        self.root = root
        self.image = image
        self._patch_pending = False
```

- [ ] **Step 5: Rewrite `_DockerHandle.apply_changes` to stage importable files**

Replace:

```python
    def apply_changes(self, changes: list[FileChange]) -> None:
        for change in changes:
            self.write_file(change.path, change.patched_content)
```

with:

```python
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
```

- [ ] **Step 6: Rewrite `_DockerHandle.run` to wrap + relax read-only when patched**

Replace the `docker_cmd = [...]` construction (the list literal at the top of `run`) with a version that conditions on `_patch_pending`:

```python
    def run(self, cmd: list[str], *, timeout_s: int = 60) -> RunResult:
        if self._patch_pending:
            # Apply the staged patches onto the installed modules inside THIS
            # container, then run the reproducer — one container so the overwrite
            # persists for the import. site-packages must be writable for the
            # copy, so --read-only is dropped for this run ONLY; all other
            # hardening (no network, no caps, --rm, tmpfs) is kept.
            readonly = []
            run_cmd = ["sh", "-c", "python /work/.tvastr_apply.py && " + shlex.join(cmd)]
        else:
            readonly = ["--read-only"]
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
```

(Only the `docker_cmd` construction + the `log.info` `patched=` field change; the `try/except` body is the existing code.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_sandbox.py -v`
Expected: PASS (all — Task 1 helper tests + the 5 Docker tests).

- [ ] **Step 8: Full suite + lint**

Run: `uv run pytest -q && uv run ruff check src tests`
Expected: all green, lint clean. (Existing verifier tests use `_FakeSandbox`, not `_DockerHandle`, so they're unaffected.)

- [ ] **Step 9: Commit**

```bash
git add src/tvastr/verification/sandbox.py tests/test_sandbox.py
git commit -m "feat(verify): land Docker patch on the installed module via single-container bootstrap"
```

---

## Final verification (after all tasks)

- [ ] `uv run pytest -q && uv run ruff check src tests` → all green, lint clean.
- [ ] **Live spot-check (success metric).** With live mode + Docker, run #21896 through `/app`, click Verify. Confirm the **rerun no longer raises the `KeyError` from the unpatched `site-packages` copy** — the bootstrap line `[tvastr] patched llama_index.core.memory.vector_memory -> /usr/.../site-packages/...` appears, and the verdict reflects the patched code (no longer `repro_broken` due to an unpatched import). The exact verdict depends on the reproducer's assertion strength (separate lever); this feature's win is that the patch is finally exercised.

## Self-Review (completed by author)

- **Spec coverage:** module derivation rule incl. the nested-integration case (Task 1 + `test_module_path_nested_integration`); Docker-only / subprocess untouched (only `_DockerHandle` edited); read-only dropped on patched rerun, other hardening kept (Task 2 Step 6 + `test_patched_rerun_is_wrapped_and_not_readonly`); staging layout `/work/.tvastr_patch/<dotted>.py` + manifest + bootstrap (Task 2 Step 5 + `test_apply_changes_stages_importable_file`); bootstrap non-fatal find_spec/skip (Step 3 `_BOOTSTRAP_SRC` + `test_bootstrap_resolves_and_copies`); no-module fallback writes repo path (Step 5 + `test_apply_changes_nonimportable_writes_repo_path`); baseline stays read-only + unwrapped (`test_baseline_run_is_readonly_and_unwrapped`); live metric in Final verification.
- **Placeholder scan:** none — every code/test step is complete.
- **Type consistency:** `installed_module_path(str) -> str | None`, `_patch_pending: bool`, `_BOOTSTRAP_SRC`, manifest shape `[[module, "<dotted>.py"]]`, staged filename `f"{module}.py"` used identically across the apply/bootstrap/tests; the bootstrap reads `/work/.tvastr_patch/{staged}` matching the staged write path.
