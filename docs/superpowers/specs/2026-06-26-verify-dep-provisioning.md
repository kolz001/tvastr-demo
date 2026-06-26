# Design Spec: Dynamic dependency provisioning for the verify sandbox

**Date:** 2026-06-26
**Branch:** `feature/verify-dep-provisioning` (off `main`)
**Status:** Proposed — pending user review

## Problem

The verify-fix loop fails with `REPRO_BROKEN` on most real issues. Root cause is
**static sandbox provisioning**, not bad fixes or bad diagnoses. `verification/
Dockerfile.llamaindex` pre-installs only a fixed handful of packages
(`llama-index` core + `llms-openai` + `vector-stores-chroma` +
`embeddings-openai` + `pytest`), but llama_index is a monorepo of hundreds of
separately-installable integration packages and the issues target the long tail.

Two failures chain from the missing package (both surface as `REPRO_BROKEN`):
1. **Patch-applier:** `_BOOTSTRAP_SRC` resolves the target via
   `importlib.util.find_spec(module)` to overwrite its `origin`. Module not
   installed → `find_spec` is None → `ModuleNotFoundError`, patch never applies.
2. **Reproducer:** behavioral oracles locate real production source via `rglob`;
   package absent → `AssertionError: could not locate ... base.py`.

Evidence (recent runs): `20d98a32` (postgres) and `10ed24b5` (s3) — both
`repro_broken`, baseline `could not locate ... base.py`, rerun
`No module named 'llama_index.vector_stores.{postgres,s3}'`. Meanwhile #21279
proved the investigator itself is healthy: `same_root_cause=true` (divergence was
fix-*strategy* only).

## Goal & success criterion

The verify sandbox installs the issue's integration package(s) on demand so the
reproducer can locate real source and the patch-applier can resolve + overwrite
the module.

**Success:** re-run an s3/postgres issue → `verify.result` is no longer
`repro_broken` for a missing-package reason; baseline locates source, patch
applies (`find_spec` resolves to the provisioned copy), rerun executes the
oracle. Verdict becomes a *real* signal (VERIFIED / STILL_BROKEN / MASKS_SYMPTOM).

## Decision (locked)

**Dynamic per-issue pip** (chosen over repo-source-install and fat-image). One
network-enabled prep `docker run` does `pip install --target /work/.tvastr_deps
<dists>`; baseline + rerun then run with `--network=none` and
`PYTHONPATH=/work/.tvastr_deps`. Covers the full integration long-tail; network
is dropped only for the prep step; reuses the existing runtime-resolver pattern.

## Design

### 1. Dist-name derivation (pure, unit-testable)
`distribution_for_path(repo_path) -> str | None`, sibling to the existing
`installed_module_path`. Rule: locate the first `llama_index` path segment (the
import root); the segment immediately before it is the distribution dir iff it
starts with `llama-index-`.
- `.../vector_stores/llama-index-vector-stores-s3/llama_index/vector_stores/s3/base.py`
  → `llama-index-vector-stores-s3`
- `llama-index-core/llama_index/core/...` → `llama-index-core` (note: `pip
  install --target` still performs a full network install of core + its dep
  tree into `.tvastr_deps` and, being prepended on PYTHONPATH, may shadow the
  image's pinned core with a possibly-different version — acceptable for a
  verify sandbox, but NOT a no-op)
- flat checkout `llama_index/core/...` or a notebook/top-level script → `None`
  (skip; nothing to provision)

The verifier collects the **set** of derived dists across all `fix.changes`
(dedup), drops `None`, and provisions them together.

### 2. Sandbox Protocol gains `provision(dists)`
Add `provision(self, dists: list[str]) -> ProvisionResult` to `SandboxHandle`
(and the `Sandbox`/handle classes). Called by the verifier right after
`prepare()`, before writing repro.py.

- **DockerHandle:** one `docker run` WITHOUT `--network=none` (default bridge),
  keeps `--rm --cap-drop=ALL --tmpfs=/tmp`, drops `--read-only` (pip writes to
  `/work` which is rw-mounted), sets `-e PIP_NO_CACHE_DIR=1 -e HOME=/tmp`, runs
  `pip install --target /work/.tvastr_deps <dists...>`. **With deps** (the
  integration may need e.g. `boto3`; the reproducer may import it). Empty dist
  list → no-op.
- **SubprocessHandle:** host `pip install --target <root>/.tvastr_deps <dists>`
  (`--target` never touches host site-packages, so the host env stays clean).
- Both then expose `/work/.tvastr_deps` (resp. `<root>/.tvastr_deps`) on
  `PYTHONPATH` for every subsequent `run()` (store the dir on the handle; inject
  via `-e PYTHONPATH=` for Docker, `env=` for subprocess). PYTHONPATH is
  **prepended** so the freshly installed integration wins.

### 3. Namespace-package merge (assumption to validate)
`llama_index` is a PEP 420 implicit namespace package, so core resolves from the
image's site-packages while `llama_index.vector_stores.s3` resolves from
`/work/.tvastr_deps` — both visible because namespace packages merge across all
`sys.path` entries. `find_spec` then returns the provisioned origin and the
bootstrap overwrites it. **Task 1 includes a smoke check** that a provisioned
integration is importable alongside core; if the merge fails we fall back to
`--target` onto a copy of site-packages (documented contingency, not expected).

### 4. Failure degradation (never crashes the run)
`provision` catches all errors (network down, nonexistent dist, pip failure),
logs, and returns `ProvisionResult(installed=[...], failed=[...], ok=bool)`. The
verifier emits a `verify.provision` event (dists requested / installed / errors)
and **proceeds regardless** — a provisioning miss degrades to today's behavior
(baseline can't reproduce → existing NO_REPRO/REPRO_BROKEN path), no regression,
no new crash surface. No new Verdict value.

### 5. Config flag
`verify_provision_deps: bool = True` (`TVASTR_VERIFY_PROVISION_DEPS`). On by
default; lets a fully-offline/hermetic run disable network provisioning. **Tests
seal it off** in `conftest.py` (`TVASTR_VERIFY_PROVISION_DEPS=false`) alongside
the existing hermetic flags so the suite never hits the network; the pure
derivation function and the event wiring are tested offline with a fake handle.

## Components

| File | Change |
|------|--------|
| `verification/sandbox.py` | `distribution_for_path` helper; `provision()` on Protocol + Docker + Subprocess handles; store deps dir + inject PYTHONPATH into every `run()`; `ProvisionResult`. |
| `verification/verifier.py` | derive dist set from `fix.changes`; call `handle.provision(...)` after `prepare()`; emit `verify.provision`; gate on `settings.verify_provision_deps`. |
| `verification/models.py` | `ProvisionResult` dataclass. |
| `config.py` | `verify_provision_deps: bool = True`. |
| `tests/conftest.py` | seal `TVASTR_VERIFY_PROVISION_DEPS=false`. |
| `tests/` | `distribution_for_path` unit tests; provision event-wiring test with a fake handle (offline); a Docker-marked smoke test (namespace merge) skipped unless docker present. |
| `api/templates/app.html` | add `verify.provision` to the verify event rendering (Minor). |

## Out of scope (explicit next steps)
- Repo-source install (`pip install -e` the version-matched monorepo subpackage)
  — more faithful but heavier; recorded as the follow-up if released wheels drift
  from the issue's source.
- Caching provisioned deps across runs (each verify reinstalls today).
- Investigator sandbox access for dynamic reproduction (prior recorded frontier).

## Testing
- `distribution_for_path`: the three derivation cases above + dedup across files.
- Provision wiring: fake handle records `provision()` called with the derived set;
  flag off → not called; provision failure → event emitted, verify proceeds.
- Docker smoke (opt-in, skipped without docker): provision `vector-stores-s3`,
  assert `llama_index.vector_stores.s3` imports alongside `llama_index.core`.
- Live metric: an s3/postgres issue no longer `repro_broken` for missing-package.
