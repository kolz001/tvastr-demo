# Design Spec: Verify-loop patch lands on the import path

**Date:** 2026-06-25
**Branch:** `feature/verify-patch-path` (off `main`)
**Status:** Approved design — ready for implementation plan

## Problem

The verify-fix loop applies the agent's patch to the **repo-relative path** but the
sandbox reproducer imports the **pip-installed** package, so the fix never takes
effect. Confirmed live on llama_index #21896:

- Patch applied to: `llama-index-core/llama_index/core/memory/vector_memory.py`
  (written to `/work/llama-index-core/...` in the sandbox).
- Reproducer does `from llama_index.core.memory ...` → Python imports
  `/usr/local/lib/python3.11/site-packages/llama_index/core/memory/vector_memory.py`.
- The patched file sits in `/work`, unimported; the rerun re-hits the original
  `KeyError` at the installed path → verdict `repro_broken`.

So **no live fix is ever actually exercised** for a pip-installed target — every
crash and behavioral verdict is computed against unpatched code. This blocks the
entire live verify signal.

A second, compounding wrinkle: each Docker `run` is a **fresh one-shot container**
with only `/work` mounted rw, so writing into `site-packages` in one `run` does
not survive to the next `run`.

## Goal & success criterion

Make the patch take effect where the reproducer imports from, so verification
runs against the patched code.

**Success (honest framing):** re-running #21896 executes the **patched**
`vector_memory.py` — the `site-packages` traceback disappears and the reproducer
imports the fixed code; the verdict then reflects real patched behavior. (Whether
that verdict is `verified_via_behavior` vs `masks_symptom` depends on the
reproducer's assertion strength — a separate repro-quality lever; this feature's
win is that the patch is finally exercised at all.)

## Scope

**In scope:** Docker sandbox only — map the patched repo path to the installed
module and overwrite it inside the same container as the rerun.

**Out of scope:** the subprocess sandbox (stays best-effort, never mutates the
host venv); reproducer assertion quality; non-pip-installed / editable-source
layouts beyond the `find_spec` resolution below.

## Decisions (locked in brainstorming)

1. **Docker-first; subprocess best-effort.** Overwriting installed packages is
   safe in Docker (ephemeral image site-packages) but would corrupt the host venv
   from the subprocess path — so subprocess keeps today's behavior unchanged and
   never touches the host venv.
2. **Runtime resolver + single-container bootstrap.** Derive the dotted module
   from the repo path; at rerun time, resolve `find_spec(module).origin` inside
   the container and copy the patched file over it, then exec the reproducer — all
   in one `docker run` so the overwrite persists for the import.

## Architecture

The verifier's sequence is unchanged (`write repro.py → baseline run →
apply_changes → rerun`). The patch-landing rides along inside `_DockerHandle`.

**Module derivation** — new pure helper `installed_module_path(repo_path) -> str | None`:
- Drop leading path segments that are not valid Python identifiers (hyphenated
  distribution dirs: `llama-index-core`, `llama-index-integrations/.../llama-index-llms-google-genai`),
  keep the importable suffix, strip `.py`, dot-join.
- `llama-index-core/llama_index/core/memory/vector_memory.py` →
  `llama_index.core.memory.vector_memory`.
- `docs/examples/x.ipynb` / a bare `script.py` → `None` (no importable package).

**Stage + bootstrap (Docker)** — `_DockerHandle.apply_changes`:
1. For each change, compute the dotted module. If it resolves, **stage** the
   patched content to `/work/.tvastr_patch/<dotted>.py` and record
   `[module, "<dotted>.py"]` in `/work/.tvastr_patch/manifest.json`; also write the
   bootstrap `/work/.tvastr_apply.py`. Files with no module are written to
   `change.path` as today (logged).
2. Set `self._patch_pending = True`.

`_DockerHandle.run`: when `_patch_pending`, wrap the command so the **same
container** applies the patch first:
`["sh", "-c", "python /work/.tvastr_apply.py && " + shlex.join(original_cmd)]`.

Because baseline runs *before* `apply_changes`, `_patch_pending` is False then →
baseline is unpatched (correct); only the rerun is wrapped.

**Read-only relaxation (security tradeoff, decided in brainstorming):** the
container normally runs `--read-only`, so `site-packages` cannot be overwritten.
For the patched rerun ONLY (`_patch_pending` true), `--read-only` is omitted so
the bootstrap can copy over the installed module. ALL other hardening is kept —
`--network=none`, `--cap-drop=ALL`, `--rm`, `--tmpfs=/tmp`. The container is
ephemeral and destroyed after the run, has no network and no capabilities, so a
writable-but-isolated ephemeral fs is a modest, contained tradeoff scoped to
exactly the run that must write the patched module. The baseline run keeps
`--read-only`.

**Bootstrap script** (`.tvastr_apply.py`):
```python
import importlib.util, json, shutil, pathlib
mani = json.loads(pathlib.Path("/work/.tvastr_patch/manifest.json").read_text())
for module, staged in mani:
    spec = importlib.util.find_spec(module)
    if spec and spec.origin:
        shutil.copyfile(f"/work/.tvastr_patch/{staged}", spec.origin)
    else:
        print(f"[tvastr] skip unresolved module: {module}")
```
Never fatal — an unresolved/uncopyable module is skipped+logged so the reproducer
always runs.

**Subprocess** unchanged: `apply_changes` writes `change.path` into its temp root
(best-effort); `run` is never wrapped; the host venv is never modified.

## Components

| File | Change |
|------|--------|
| `src/tvastr/verification/sandbox.py` | `installed_module_path()` helper; `_DockerHandle.__init__` adds `_patch_pending=False`; `_DockerHandle.apply_changes` stages files + manifest + bootstrap; `_DockerHandle.run` wraps cmd when `_patch_pending`. `_SubprocessHandle` untouched. |
| `tests/test_sandbox.py` (create) | helper units + Docker apply/run-wrapping tests (mock `subprocess.run`) |

## Data flow (#21896, Docker)

```
write repro.py (behavioral; asserts round-trip; ends with marker)
baseline run  (_patch_pending=False) → KeyError at site-packages/.../vector_memory.py:150 → reproduced
apply_changes(fix on llama-index-core/.../vector_memory.py)
   module = llama_index.core.memory.vector_memory
   stage /work/.tvastr_patch/llama_index.core.memory.vector_memory.py + manifest + bootstrap
   _patch_pending = True
rerun → sh -c "python /work/.tvastr_apply.py && python repro.py"
   bootstrap: find_spec(module).origin → copy staged file over the INSTALLED module
   repro imports the PATCHED module → verdict reflects patched behavior (no longer repro_broken-from-unpatched)
```

## Edge cases / safety

- **`find_spec` unresolved** → bootstrap skips+logs; reproducer runs against the
  unpatched install (today's behavior for that file). No crash.
- **Non-importable path** (notebook, bare script) → helper returns `None` → write
  `change.path` (harmless; notebooks already excluded as edit targets upstream).
- **Multiple changed files** → all staged; manifest lists each; bootstrap applies all.
- **Bootstrap copy failure on one module** → logged, loop continues; reproducer
  still runs.
- **Subprocess sandbox** → unchanged; never writes host site-packages.
- `shlex.join` for safe command wrapping.

## Testing (offline — mock `subprocess.run`, no real Docker)

- **`installed_module_path`:** repo core path → `llama_index.core.memory.vector_memory`;
  google-genai integration path → `llama_index.llms.google_genai.utils`;
  `docs/x.ipynb` → `None`; bare `script.py` → `None`.
- **Docker apply:** `apply_changes` stages each importable file under
  `/work/.tvastr_patch/<dotted>.py`, writes `manifest.json` + `.tvastr_apply.py`,
  sets `_patch_pending`; a non-importable file is written to `change.path`.
- **Docker run wrapping:** baseline (before apply) issues a plain docker cmd
  containing `--read-only`; after `apply_changes`, the docker cmd contains
  `.tvastr_apply.py && python repro.py`, OMITS `--read-only`, and still contains
  `--network=none`, `--cap-drop=ALL`, `--rm`.
- **Subprocess unchanged:** `apply_changes` writes `change.path`; `run` not wrapped.
- **Bootstrap content:** generated `.tvastr_apply.py` resolves via `find_spec` and
  copies over `.origin`.
- **Live metric:** #21896 rerun executes patched `vector_memory.py` (no
  site-packages KeyError from unpatched code); verdict reflects patched behavior.

## Files (anticipated)

| File | Change |
|------|--------|
| `src/tvastr/verification/sandbox.py` | helper + Docker staging/bootstrap/run-wrap |
| `tests/test_sandbox.py` | helper + Docker apply/run tests |
