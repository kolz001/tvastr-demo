# Verify Dependency Provisioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the verify sandbox install an issue's llama_index integration package(s) on demand so reproducers can locate real source and the patch-applier can resolve + overwrite the module — eliminating the `REPRO_BROKEN`-from-missing-package flood.

**Architecture:** A pure path→distribution helper derives pip dist names from the fix's file paths. The sandbox handle gains a `provision(dists)` step (one network-enabled `pip install --target /work/.tvastr_deps` for Docker; host `pip install --target` for subprocess) that runs right after `prepare()`; every subsequent `run()` injects that dir on `PYTHONPATH` (prepended). Baseline + rerun stay `--network=none`. Provisioning never crashes the run — failure degrades to today's behavior.

**Tech Stack:** Python 3.11/3.12, pydantic-settings, Docker CLI, pytest, `importlib`/`pip`.

## Global Constraints

- **Never crash the verify run.** `provision` catches all errors and returns a `ProvisionResult`; the verifier proceeds regardless of provisioning outcome. No new `Verdict` value.
- **Network only for the prep step.** The provision `docker run` drops `--network=none` (and `--read-only`) but keeps `--rm --cap-drop=ALL --tmpfs=/tmp`. Baseline + rerun runs remain `--network=none` (Docker) and keep all existing hardening.
- **PYTHONPATH is prepended** so the freshly provisioned integration wins over any image copy; rely on `llama_index` being a PEP 420 namespace package so core + integration coexist.
- **Tests never hit the network.** Seal `TVASTR_VERIFY_PROVISION_DEPS=false` in `tests/conftest.py`; unit-test the pure derivation offline and the install path with a monkeypatched/fake subprocess.
- **Config flag** `verify_provision_deps: bool = True`, env `TVASTR_VERIFY_PROVISION_DEPS`, default on.
- **Distribution dir rule:** the path segment immediately before the first `llama_index` import-root segment, returned only when it starts with `llama-index-`.
- Env prefix is `TVASTR_`; AgentContext does NOT carry full Settings (flags are read at construction sites and passed in, like `verify_project_root`).
- Commit footer on every commit:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
  `Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi`

---

### Task 1: Provisioning mechanism — `distribution_for_path`, `ProvisionResult`, `provision()` on sandbox handles

**Files:**
- Modify: `src/tvastr/verification/models.py` (add `ProvisionResult`)
- Modify: `src/tvastr/verification/sandbox.py` (add `distribution_for_path`, `provision()` on Protocol + both handles, PYTHONPATH injection in `run()`)
- Test: `tests/test_sandbox_provision.py` (new)

**Interfaces:**
- Consumes: existing `RunResult`, `_SubprocessHandle`, `_DockerHandle`, `installed_module_path`, `SandboxHandle` Protocol.
- Produces:
  - `distribution_for_path(repo_path: str) -> str | None`
  - `ProvisionResult(requested: list[str], installed: list[str], failed: list[str], ok: bool)` (frozen dataclass in `models.py`)
  - `SandboxHandle.provision(self, dists: list[str]) -> ProvisionResult`
  - After a successful provision, every `handle.run(...)` exposes the deps dir on `PYTHONPATH`.

- [ ] **Step 1: Write the failing test for `distribution_for_path` + `ProvisionResult`**

Create `tests/test_sandbox_provision.py`:

```python
from __future__ import annotations

import subprocess

from tvastr.verification.models import ProvisionResult
from tvastr.verification.sandbox import (
    SubprocessSandbox,
    distribution_for_path,
)


def test_distribution_for_path_integration():
    p = (
        "llama-index-integrations/vector_stores/"
        "llama-index-vector-stores-s3/llama_index/vector_stores/s3/base.py"
    )
    assert distribution_for_path(p) == "llama-index-vector-stores-s3"


def test_distribution_for_path_core():
    assert (
        distribution_for_path("llama-index-core/llama_index/core/base.py")
        == "llama-index-core"
    )


def test_distribution_for_path_flat_checkout_returns_none():
    # No integration dir above the import root → nothing to provision.
    assert distribution_for_path("llama_index/core/base.py") is None


def test_distribution_for_path_non_package_returns_none():
    assert distribution_for_path("examples/demo.ipynb") is None
    assert distribution_for_path("README.md") is None


def test_provision_result_empty_is_ok():
    r = ProvisionResult(requested=[], installed=[], failed=[], ok=True)
    assert r.ok and r.installed == []
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/test_sandbox_provision.py -v`
Expected: FAIL — `ImportError: cannot import name 'distribution_for_path'` / `ProvisionResult`.

- [ ] **Step 3: Add `ProvisionResult` to `models.py`**

In `src/tvastr/verification/models.py`, after the `RunResult` dataclass (around line 57), add:

```python
@dataclass(frozen=True)
class ProvisionResult:
    """Outcome of installing an issue's integration package(s) into a sandbox."""

    requested: list[str]
    installed: list[str]
    failed: list[str]
    ok: bool
```

- [ ] **Step 4: Add `distribution_for_path` to `sandbox.py`**

In `src/tvastr/verification/sandbox.py`, add `import os` and `import sys` to the imports block (alongside `import json`, `import shlex`, etc.), import `ProvisionResult` from models:

```python
from tvastr.verification.models import ProvisionResult, RunResult
```

Then add this function right after `installed_module_path` (after line 70):

```python
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
```

- [ ] **Step 5: Run derivation tests to confirm they pass**

Run: `uv run pytest tests/test_sandbox_provision.py -k distribution_or_provision_result -v` then the full file:
Run: `uv run pytest tests/test_sandbox_provision.py -v`
Expected: the 4 `distribution_for_path` tests + `test_provision_result_empty_is_ok` PASS.

- [ ] **Step 6: Write the failing test for subprocess `provision()` + PYTHONPATH injection**

Append to `tests/test_sandbox_provision.py`:

```python
def test_subprocess_provision_installs_and_injects_pythonpath(monkeypatch):
    sandbox = SubprocessSandbox()
    handle = sandbox.prepare()
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # Simulate a successful `pip install --target` creating the deps dir.
        (handle.root / ".tvastr_deps").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = handle.provision(["llama-index-vector-stores-s3"])
    assert result.ok
    assert result.installed == ["llama-index-vector-stores-s3"]
    assert "install" in captured["cmd"] and "--target" in captured["cmd"]

    # After provisioning, a real run must expose the deps dir on PYTHONPATH.
    monkeypatch.undo()
    out = handle.run(
        ["python", "-c", "import os; print(os.environ.get('PYTHONPATH', ''))"]
    )
    assert str(handle.root / ".tvastr_deps") in out.stdout
    handle.discard()


def test_subprocess_provision_empty_is_noop(monkeypatch):
    sandbox = SubprocessSandbox()
    handle = sandbox.prepare()

    def boom(*a, **k):  # provision must not invoke pip for an empty dist list
        raise AssertionError("pip should not run for empty dists")

    monkeypatch.setattr(subprocess, "run", boom)
    result = handle.provision([])
    assert result.ok and result.requested == []
    handle.discard()


def test_subprocess_provision_failure_reports_not_ok(monkeypatch):
    sandbox = SubprocessSandbox()
    handle = sandbox.prepare()

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No matching distribution")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = handle.provision(["llama-index-does-not-exist"])
    assert not result.ok
    assert result.failed == ["llama-index-does-not-exist"]
    handle.discard()
```

- [ ] **Step 7: Run to confirm it fails**

Run: `uv run pytest tests/test_sandbox_provision.py -k provision -v`
Expected: FAIL — `_SubprocessHandle` has no attribute `provision`.

- [ ] **Step 8: Implement `provision()` + PYTHONPATH on `_SubprocessHandle`**

In `src/tvastr/verification/sandbox.py`, update `_SubprocessHandle`:

Change `__init__` to track a deps dir:

```python
    def __init__(self, root: Path) -> None:
        self.root = root
        self._deps_dir: Path | None = None
```

Add a private env helper and the `provision` method (place above `run`):

```python
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
        return ProvisionResult(requested=dists, installed=[], failed=dists, ok=False)
```

Update `run` to pass the env (change the `subprocess.run(...)` call):

```python
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
```

(Note: `env=None` makes `subprocess.run` inherit the parent environment unchanged — identical to today's behavior when nothing is provisioned.)

- [ ] **Step 9: Run subprocess provision tests to confirm they pass**

Run: `uv run pytest tests/test_sandbox_provision.py -v`
Expected: all PASS.

- [ ] **Step 10: Add `provision()` to the `SandboxHandle` Protocol and `_DockerHandle`**

In the `SandboxHandle` Protocol (around line 79), add the method signature:

```python
    def write_file(self, relpath: str, content: str) -> None: ...
    def apply_changes(self, changes: list[FileChange]) -> None: ...
    def provision(self, dists: list[str]) -> ProvisionResult: ...
    def run(self, cmd: list[str], *, timeout_s: int = 60) -> RunResult: ...
    def discard(self) -> None: ...
```

In `_DockerHandle.__init__`, add a provisioned flag:

```python
    def __init__(self, root: Path, image: str) -> None:
        self.root = root
        self.image = image
        self._patch_pending = False
        self._deps_provisioned = False
```

Add the `provision` method to `_DockerHandle` (place above `run`). The prep run keeps all hardening EXCEPT `--network=none` and `--read-only`:

```python
    def provision(self, dists: list[str]) -> ProvisionResult:
        dists = [d for d in dict.fromkeys(dists) if d]
        if not dists:
            return ProvisionResult(requested=[], installed=[], failed=[], ok=True)
        docker_cmd = [
            "docker", "run", "--rm",
            "--cap-drop=ALL",
            "--tmpfs=/tmp:rw,size=256m",
            "-e", "PIP_NO_CACHE_DIR=1",
            "-e", "HOME=/tmp",
            "-v", f"{self.root}:/work:rw",
            "-w", "/work",
            self.image,
            "pip", "install", "--target", "/work/.tvastr_deps", *dists,
        ]
        log.info("verify.sandbox.provision", sandbox="docker", dists=dists, image=self.image)
        try:
            proc = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=300, check=False)
        except Exception as exc:
            log.warning("verify.sandbox.provision.error", error=str(exc))
            return ProvisionResult(requested=dists, installed=[], failed=dists, ok=False)
        if proc.returncode == 0:
            self._deps_provisioned = True
            return ProvisionResult(requested=dists, installed=dists, failed=[], ok=True)
        log.warning("verify.sandbox.provision.failed", stderr=proc.stderr[-400:])
        if (self.root / ".tvastr_deps").exists():
            self._deps_provisioned = True
        return ProvisionResult(requested=dists, installed=[], failed=dists, ok=False)
```

Update `_DockerHandle.run` to inject `PYTHONPATH` when deps were provisioned. Replace the body up to the `docker_cmd` assignment with:

```python
    def run(self, cmd: list[str], *, timeout_s: int = 60) -> RunResult:
        env_flags = (
            ["-e", "PYTHONPATH=/work/.tvastr_deps"] if self._deps_provisioned else []
        )
        if self._patch_pending:
            # Apply staged patches onto the installed modules inside THIS
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
            *env_flags,
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

(The bootstrap `_BOOTSTRAP_SRC` is unchanged — with `PYTHONPATH=/work/.tvastr_deps` in the container env, its `importlib.util.find_spec(module)` resolves to the provisioned copy and overwrites it.)

- [ ] **Step 11: Run full sandbox tests + lint**

Run: `uv run pytest tests/test_sandbox_provision.py tests/test_verifier.py tests/test_list_dir.py -v`
Expected: all PASS (existing sandbox/verifier tests unaffected).
Run: `uv run ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 12: Commit**

```bash
git add src/tvastr/verification/models.py src/tvastr/verification/sandbox.py tests/test_sandbox_provision.py
git commit -m "feat(verify): on-demand dependency provisioning in the sandbox

distribution_for_path derives the pip dist from a fix path; SandboxHandle
gains provision(dists) (pip install --target .tvastr_deps) and injects that
dir on PYTHONPATH for every subsequent run. Docker prep run drops network
isolation for the install step only; baseline/rerun stay --network=none.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi"
```

---

### Task 2: Wire provisioning into the verifier (flag, derivation, event, degradation)

**Files:**
- Modify: `src/tvastr/config.py` (add `verify_provision_deps`)
- Modify: `src/tvastr/verification/verifier.py` (`provision_deps` kwarg; derive dists; call `provision`; emit `verify.provision`; degrade on failure)
- Modify: `src/tvastr/api/routes/verify.py:176` (pass `provision_deps=settings.verify_provision_deps`)
- Modify: `tests/conftest.py` (seal `TVASTR_VERIFY_PROVISION_DEPS=false`)
- Test: `tests/test_verifier.py` (add `provision()` to `_FakeHandle`; new wiring tests)

**Interfaces:**
- Consumes: `distribution_for_path`, `ProvisionResult`, `SandboxHandle.provision` (Task 1); `Verifier.__init__`, `Verifier.verify`, `self._emit`, `fix.changes` (each `FileChange` has `.path`).
- Produces: `Verifier(..., provision_deps: bool = True)`; a `verify.provision` event with payload `{requested, installed, failed, ok}`; `settings.verify_provision_deps: bool`.

- [ ] **Step 1: Add the config flag**

In `src/tvastr/config.py`, after `verify_project_root` (line 101), add:

```python
    # When true, the verify sandbox installs the issue's integration package(s)
    # on demand (pip install --target) before running the reproducer, so the
    # long tail of llama_index integrations is importable. Needs network for the
    # prep step; baseline/rerun stay network-isolated. Off in tests.
    verify_provision_deps: bool = True
```

- [ ] **Step 2: Seal the flag in conftest**

In `tests/conftest.py`, after the `TVASTR_DRY_RUN` block (line 22), add:

```python
# Seal the verify-provisioning flag — a developer's .env may enable it, which
# would make the verify sandbox pip-install over the network during tests.
os.environ["TVASTR_VERIFY_PROVISION_DEPS"] = "false"
```

- [ ] **Step 3: Add `provision()` to the test `_FakeHandle`**

In `tests/test_verifier.py`, in `_FakeHandle.__init__`, add a record list:

```python
        self.provisioned: list[list[str]] = []
        self.provision_ok = True
```

And add the method (after `apply_changes`):

```python
    def provision(self, dists):
        from tvastr.verification.models import ProvisionResult

        self.provisioned.append(list(dists))
        ok = self.provision_ok
        return ProvisionResult(
            requested=list(dists),
            installed=list(dists) if ok else [],
            failed=[] if ok else list(dists),
            ok=ok,
        )
```

- [ ] **Step 4: Write the failing wiring tests**

Add to `tests/test_verifier.py` (these reuse the existing `_FakeSandbox`/`_ctx` helpers; check the file for the exact `RunResult` sequence other tests pass so baseline reproduces then rerun is clean — mirror an existing passing test's `responses`). Use integration-style paths so `distribution_for_path` yields dists:

```python
def test_verify_provisions_derived_distributions(monkeypatch):
    # Two files in the same dist + one in another → two deduped, sorted dists.
    changes = [
        FileChange(
            path="llama-index-integrations/vector_stores/llama-index-vector-stores-s3/"
            "llama_index/vector_stores/s3/base.py",
            patched_content="# fixed\n",
            rationale="r",
        ),
        FileChange(
            path="llama-index-integrations/vector_stores/llama-index-vector-stores-s3/"
            "llama_index/vector_stores/s3/utils.py",
            patched_content="# fixed2\n",
            rationale="r",
        ),
        FileChange(
            path="llama-index-integrations/vector_stores/llama-index-vector-stores-postgres/"
            "llama_index/vector_stores/postgres/base.py",
            patched_content="# fixed3\n",
            rationale="r",
        ),
    ]
    sandbox = _FakeSandbox(responses=[...])  # baseline reproduces, rerun clean — mirror existing test
    verifier = Verifier(_ctx(...), sandbox, provision_deps=True)
    verifier.verify(_pattern(), _root_cause(changes), _fix(changes), [], "body")
    assert sandbox.last_handle.provisioned == [
        ["llama-index-vector-stores-postgres", "llama-index-vector-stores-s3"]
    ]


def test_verify_does_not_provision_when_flag_off():
    changes = [FileChange(path="llama-index-integrations/.../llama_index/vector_stores/s3/base.py",
                          patched_content="# fixed\n", rationale="r")]
    sandbox = _FakeSandbox(responses=[...])
    verifier = Verifier(_ctx(...), sandbox, provision_deps=False)
    verifier.verify(_pattern(), _root_cause(changes), _fix(changes), [], "body")
    assert sandbox.last_handle.provisioned == []


def test_verify_emits_provision_event_and_proceeds_on_failure():
    changes = [FileChange(path="llama-index-integrations/.../llama_index/vector_stores/s3/base.py",
                          patched_content="# fixed\n", rationale="r")]
    sink = _RecordingSink()  # use the sink helper the existing event test uses
    sandbox = _FakeSandbox(responses=[...])
    sandbox._provision_ok = False  # set via handle below
    verifier = Verifier(_ctx(...), sandbox, event_sink=sink, run_id="p1", provision_deps=True)
    # Make the prepared handle report provision failure:
    # (prepare() returns the handle lazily — set the flag on _FakeSandbox so
    #  prepared handles inherit it; add that pass-through in _FakeSandbox.prepare)
    verifier.verify(_pattern(), _root_cause(changes), _fix(changes), [], "body")
    types = [e.type for e in sink.events]
    assert "verify.provision" in types
    # verify still ran the baseline despite provision failure:
    assert "verify.baseline" in types
```

> Implementer note: adapt the `responses=[...]`, `_ctx(...)`, `_pattern()`,
> `_root_cause`, `_fix`, and sink helpers to whatever the existing tests in
> `tests/test_verifier.py` already use (read the file first). The assertions
> above are the contract; the scaffolding must match the file's conventions.
> For the failure test, add a `provision_ok` pass-through to `_FakeSandbox`
> (store it and set `handle.provision_ok = self._provision_ok` in `prepare`).

- [ ] **Step 5: Run to confirm the new tests fail**

Run: `uv run pytest tests/test_verifier.py -k provision -v`
Expected: FAIL — `Verifier.__init__` has no `provision_deps`; no `verify.provision` event.

- [ ] **Step 6: Add `provision_deps` to `Verifier.__init__`**

In `src/tvastr/verification/verifier.py`, update `__init__` (lines 61-74):

```python
    def __init__(
        self,
        ctx: AgentContext,
        sandbox: Sandbox,
        *,
        project_root: Path | None = None,
        event_sink: EventSink | None = None,
        run_id: str | None = None,
        provision_deps: bool = True,
    ) -> None:
        self.ctx = ctx
        self.sandbox = sandbox
        self.project_root = project_root  # for scoped-test discovery
        self.sink = event_sink or NullEventSink()
        self.run_id = run_id
        self.provision_deps = provision_deps
```

- [ ] **Step 7: Derive + provision + emit in `verify()`**

Add the import at the top of `verifier.py` (with the other `from tvastr.verification...` imports):

```python
from tvastr.verification.sandbox import distribution_for_path
```

In `verify()`, immediately after `handle = self.sandbox.prepare()` (line 142) and inside the existing `try:` (line 143), before `handle.write_file("repro.py", ...)`, insert:

```python
            # 2a. Provision the issue's integration package(s) so the reproducer
            # can locate real source and the patch-applier can resolve the
            # module. Never fatal: a miss degrades to the existing no-repro path.
            if self.provision_deps:
                try:
                    dists = sorted(
                        {
                            d
                            for c in fix.changes
                            if (d := distribution_for_path(c.path)) is not None
                        }
                    )
                    if dists:
                        pr = handle.provision(dists)
                        self._emit(
                            "verify.provision",
                            "verify",
                            {
                                "requested": pr.requested,
                                "installed": pr.installed,
                                "failed": pr.failed,
                                "ok": pr.ok,
                            },
                        )
                except Exception as exc:  # provisioning must never abort verify
                    log.warning("verify.provision.error", error=str(exc))
```

- [ ] **Step 8: Pass the flag at the production construction site**

In `src/tvastr/api/routes/verify.py`, update the `Verifier(...)` call (line 176):

```python
    verifier = Verifier(
        ctx,
        sandbox,
        project_root=project_root,
        event_sink=sink,
        run_id=run_id,
        provision_deps=settings.verify_provision_deps,
    )
```

- [ ] **Step 9: Run the wiring tests + full verifier suite**

Run: `uv run pytest tests/test_verifier.py -v`
Expected: all PASS (new provision tests + existing).

- [ ] **Step 10: Run the full suite + lint**

Run: `uv run pytest -q`
Expected: all green (report actual count).
Run: `uv run ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 11: Commit**

```bash
git add src/tvastr/config.py src/tvastr/verification/verifier.py src/tvastr/api/routes/verify.py tests/conftest.py tests/test_verifier.py
git commit -m "feat(verify): wire on-demand dep provisioning into the verifier

Verifier derives the dist set from fix.changes, calls handle.provision before
baseline, emits verify.provision, and proceeds regardless of outcome. Gated by
verify_provision_deps (default on; sealed off in tests).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi"
```

---

### Task 3: Render `verify.provision` in the dashboard

**Files:**
- Modify: `src/tvastr/api/templates/app.html` (verify event allowlist line ~226; summary switch line ~522)

**Interfaces:**
- Consumes: the `verify.provision` event emitted in Task 2 (payload `{requested, installed, failed, ok}`).
- Produces: a human-readable verify-timeline row for provisioning.

- [ ] **Step 1: Add `verify.provision` to the verify event allowlist**

In `src/tvastr/api/templates/app.html`, find the verify event list (around lines 226-227):

```javascript
  "verify.start","verify.repro_synth","verify.baseline",
  "verify.patch_applied","verify.rerun","verify.regression","verify.result"
```

Change it to include provisioning right after `verify.start`:

```javascript
  "verify.start","verify.provision","verify.repro_synth","verify.baseline",
  "verify.patch_applied","verify.rerun","verify.regression","verify.result"
```

- [ ] **Step 2: Add a summary label for the event**

In the summary switch (around lines 522-528), after the `case "verify.start":` line, add:

```javascript
    case "verify.provision": return `provisioned ${(p.installed||[]).length}/${(p.requested||[]).length} dep(s)${p.ok?"":" · FAILED"}${(p.requested||[]).length?` · ${(p.requested||[]).join(", ")}`:""}`;
```

- [ ] **Step 3: Manually verify the page still parses**

Run: `uv run python -c "from pathlib import Path; import tvastr.api.app as a; html = (Path(a.__file__).resolve().parent / 'templates' / 'app.html').read_text(); assert 'verify.provision' in html; print('ok, present twice:', html.count('verify.provision'))"`
Expected: `ok, present twice: 2`

- [ ] **Step 4: Run the suite (no behavior change, sanity only) + lint**

Run: `uv run pytest -q`
Expected: all green.
Run: `uv run ruff check src tests`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(verify): surface verify.provision in the dashboard timeline

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_012aNa2Yz6hvduRFAsPCHdTi"
```

---

## Self-Review

**Spec coverage:**
- Dynamic per-issue pip → Task 1 (`provision` + `--target`) + Task 2 (derivation + call). ✓
- `distribution_for_path` derivation rule → Task 1. ✓
- `provision()` on Protocol + Docker + Subprocess, PYTHONPATH prepend → Task 1. ✓
- Network only for prep step (Docker drops `--network=none`/`--read-only`, keeps rest) → Task 1 Step 10. ✓
- Namespace-merge assumption → relied on; validated live (Docker smoke deferred to live run, noted in spec). ✓
- Never crashes / degrade → Task 1 (provision catches) + Task 2 Step 7 (try/except + proceed). ✓
- Config flag + conftest seal → Task 2 Steps 1-2. ✓
- `verify.provision` event + UI → Task 2 Step 7 + Task 3. ✓
- `ProvisionResult` → Task 1 Step 3. ✓
- Out-of-scope items (repo-source install, caching) correctly omitted. ✓

**Placeholder scan:** Task 2 Step 4 intentionally leaves `responses=[...]`/helper scaffolding to match the existing test file's conventions (the implementer must read `tests/test_verifier.py`); the assertions are concrete and binding. All production code blocks are complete.

**Type consistency:** `ProvisionResult(requested, installed, failed, ok)` used identically in models, both handles, the fake, and the event payload. `provision(self, dists: list[str]) -> ProvisionResult` consistent across Protocol + handles + fake. `provision_deps` consistent in `__init__`, construction site, and tests.
