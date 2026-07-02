# SDK-Schema Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a diagnosis hinges on a third-party response shape, install the SDK (host pip, wheels-only) and inject its relevant type definitions into the doc-grounding prompt as ground truth for field names.

**Architecture:** A new pure module `agent/sdk_schema.py` (probe-prompt + parse/validate, `fetch_sdk` via wheels-only pip into a `data/sdk_cache/` cache, deterministic `extract_schema_snippets` over the installed package). `graph.py`'s `_ground_root_cause` calls a small `_sdk_schema_evidence` helper (probe LLM call → fetch → extract → emit `doc.sdk_schema`) and prepends the returned block to the existing grounding prompt. Every failure rung degrades to today's exact behavior.

**Tech Stack:** Python 3.12, pydantic-settings config, pytest with tmp_path/monkeypatch (no network in tests), vanilla-JS dashboard template.

**Spec:** `docs/superpowers/specs/2026-07-02-sdk-schema-grounding-design.md`

## Global Constraints

- The probe's package name is LLM-supplied: validate against `^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$` before ANY subprocess; pip runs via list-form `subprocess.run` (never `shell=True`).
- pip flags exactly: `--only-binary=:all: --no-deps --target <dir> --quiet` (wheels only, no transitive deps, isolated dir). A failing version pin is retried ONCE without the pin.
- Nothing under `data/sdk_cache/` is ever imported or executed — snippets are read as text only.
- Degradation ladder: probe `relevant:false` / unparseable probe / invalid name / pip failure / zero matches → emit `doc.sdk_schema` with `ok:false` + `reason`, return no block, grounding proceeds unchanged. No new code path may raise out of `_ground_root_cause`.
- Flag `sdk_schema_grounding` (env `TVASTR_SDK_SCHEMA_GROUNDING`) default `True` in `Settings`, `False` in `AgentContext` (mirrors `doc_grounding`/`issue_era_retrieval`), sealed `"false"` in `tests/conftest.py`. Flag off → zero probe calls.
- Snippet caps: `max_snippets=6`, `max_lines_each=40`; keywords capped at 4.
- New TaskType member `SCHEMA_PROBE = "schema_probe"` only — no `_LOCAL_TASKS` change (non-local tasks already route to cloud with redaction).
- Tests must not touch the network; `uv run pytest` green and `uv run ruff check src tests` clean at every commit.
- Work on branch `feature/sdk-schema-grounding` off `main`.

## File anchors

- `src/tvastr/llm/router.py:35-44` — `TaskType` StrEnum (add member after `REPRO_CRITIQUE`).
- `src/tvastr/config.py:46-51` — `doc_grounding` / `issue_era_retrieval` block (add flag after `issue_era_retrieval`).
- `src/tvastr/agent/context.py:66-67` — `doc_grounding: bool = False` / `issue_era_retrieval: bool = False` fields.
- `src/tvastr/pipeline.py:~261-271` — `AgentContext(...)` construction passing `doc_grounding=...`, `issue_era_retrieval=...`.
- `src/tvastr/agent/graph.py:319-365` — `_ground_root_cause` (prompt built at 326-335; injection goes between the guard and the prompt).
- `tests/conftest.py:19-20` — seal block (`TVASTR_DOC_GROUNDING`, `TVASTR_ISSUE_ERA_RETRIEVAL`).
- `.gitignore:28-30` — `data/...` entries.
- `src/tvastr/api/templates/app.html` — `summarize(event)` switch, `case "doc.grounded"` / `case "doc.skipped"` lines.
- Reuse: `extract_all_json` from `tvastr.analysis._jsonutil`; test conventions (`_SeqRouter`, `_FakeHost`, `_agent`) from `tests/test_agent_investigate.py` and `tests/test_agent_grounding.py`.

---

### Task 1: `sdk_schema.py` module (probe parse, fetch, extract, format)

**Files:**
- Create: `src/tvastr/agent/sdk_schema.py`
- Test: `tests/test_sdk_schema.py`

**Interfaces:**
- Consumes: `tvastr.analysis._jsonutil.extract_all_json`, `tvastr.logging.get_logger`.
- Produces (Task 2 relies on these exact signatures):
  - `@dataclass(frozen=True) SchemaProbe(package: str, version_hint: str | None, keywords: tuple[str, ...])`
  - `@dataclass(frozen=True) Snippet(path: str, text: str)`
  - `build_probe_prompt(title: str, summary: str, suspected_files: list[str], issue_snippet: str) -> str`
  - `PROBE_SYSTEM: str` (module constant)
  - `parse_probe(text: str) -> SchemaProbe | None` (None ⇔ not relevant / unparseable / invalid)
  - `fetch_sdk(package: str, version_hint: str | None, cache_root: Path = Path("data/sdk_cache")) -> Path | None`
  - `extract_schema_snippets(root: Path, keywords: Sequence[str], max_snippets: int = 6, max_lines_each: int = 40) -> list[Snippet]`
  - `format_schema_block(package: str, snippets: list[Snippet]) -> str`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sdk_schema.py`:

```python
"""Tests for SDK-schema grounding primitives (no network)."""

from __future__ import annotations

from pathlib import Path

from tvastr.agent.sdk_schema import (
    SchemaProbe,
    Snippet,
    build_probe_prompt,
    extract_schema_snippets,
    fetch_sdk,
    format_schema_block,
    parse_probe,
)


# --- parse_probe ---

def test_parse_probe_valid():
    text = (
        '{"relevant": true, "package": "google-genai", "version_hint": "1.2.0",'
        ' "keywords": ["usage_metadata", "candidates_token_count"]}'
    )
    probe = parse_probe(text)
    assert probe == SchemaProbe(
        package="google-genai",
        version_hint="1.2.0",
        keywords=("usage_metadata", "candidates_token_count"),
    )


def test_parse_probe_not_relevant_returns_none():
    assert parse_probe('{"relevant": false, "package": "x", "keywords": ["k"]}') is None


def test_parse_probe_garbage_returns_none():
    assert parse_probe("no json here") is None
    assert parse_probe('{"relevant": true}') is None  # missing package/keywords


def test_parse_probe_rejects_malicious_package_names():
    for bad in ('pkg; rm -rf /', '../../etc', 'a b', '-flag', 'x' * 100, ''):
        text = f'{{"relevant": true, "package": "{bad}", "keywords": ["k"]}}'
        assert parse_probe(text) is None, bad


def test_parse_probe_caps_keywords_at_four_and_stringifies():
    text = (
        '{"relevant": true, "package": "p",'
        ' "keywords": ["a", "b", "c", "d", "e", 7]}'
    )
    probe = parse_probe(text)
    assert probe is not None
    assert probe.keywords == ("a", "b", "c", "d")


def test_parse_probe_merges_multiple_json_objects():
    # Models sometimes emit prose + a JSON object; reuse the multi-object parser.
    text = 'Thinking...\n{"relevant": true, "package": "p", "keywords": ["k"]}'
    probe = parse_probe(text)
    assert probe is not None and probe.package == "p"


# --- extract_schema_snippets ---

def _fake_pkg(tmp_path: Path) -> Path:
    root = tmp_path / "cache" / "google-genai-latest"
    pkg = root / "google" / "genai"
    pkg.mkdir(parents=True)
    (pkg / "types.py").write_text(
        "class Unrelated:\n    x: int = 0\n\n\n"
        "class GenerateContentResponseUsageMetadata:\n"
        "    prompt_token_count: int | None = None\n"
        "    candidates_token_count: int | None = None\n"
        "    response_token_count: int | None = None\n\n\n"
        "class AlsoUnrelated:\n    y: int = 0\n"
    )
    (pkg / "stubs.pyi").write_text(
        "class StubResponse:\n    usage_metadata: object\n"
    )
    return root


def test_extract_finds_class_containing_keyword(tmp_path):
    root = _fake_pkg(tmp_path)
    snips = extract_schema_snippets(root, ["candidates_token_count"])
    assert len(snips) == 1
    assert "GenerateContentResponseUsageMetadata" in snips[0].text
    assert "response_token_count" in snips[0].text  # the whole class block
    assert snips[0].path.endswith("types.py")


def test_extract_searches_pyi_files(tmp_path):
    root = _fake_pkg(tmp_path)
    snips = extract_schema_snippets(root, ["usage_metadata"])
    paths = {s.path for s in snips}
    assert any(p.endswith("stubs.pyi") for p in paths)


def test_extract_no_match_returns_empty(tmp_path):
    root = _fake_pkg(tmp_path)
    assert extract_schema_snippets(root, ["definitely_absent_zzz"]) == []


def test_extract_caps_snippets_and_lines(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    big = "\n".join(f"    field_{i}: int = {i}" for i in range(100))
    (root / "m.py").write_text(
        "\n\n".join(f"class C{i}:\n    keyword_hit = 1\n{big}" for i in range(10))
    )
    snips = extract_schema_snippets(root, ["keyword_hit"], max_snippets=3, max_lines_each=5)
    assert len(snips) == 3
    for s in snips:
        assert len(s.text.splitlines()) <= 6  # 5 lines + truncation marker
        assert s.text.splitlines()[-1].strip() == "..."


# --- fetch_sdk (subprocess mocked; cache behavior real) ---

def test_fetch_sdk_builds_wheels_only_pip_argv(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        Path(argv[argv.index("--target") + 1]).mkdir(parents=True, exist_ok=True)
        (Path(argv[argv.index("--target") + 1]) / "pkg").mkdir(exist_ok=True)

        class R:
            returncode = 0
            stderr = b""
        return R()

    monkeypatch.setattr("tvastr.agent.sdk_schema.subprocess.run", fake_run)
    out = fetch_sdk("google-genai", "1.2.0", cache_root=tmp_path)
    assert out is not None and out.is_dir()
    argv = calls[0]
    assert "--only-binary=:all:" in argv
    assert "--no-deps" in argv
    assert "google-genai==1.2.0" in argv
    assert argv[0].endswith("python") or "python" in argv[0]


def test_fetch_sdk_retries_without_failing_pin(tmp_path, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)

        class R:
            returncode = 1 if any("==" in a for a in argv) else 0
            stderr = b"no matching distribution"
        if R.returncode == 0:
            t = Path(argv[argv.index("--target") + 1])
            t.mkdir(parents=True, exist_ok=True)
            (t / "pkg").mkdir(exist_ok=True)
        return R()

    monkeypatch.setattr("tvastr.agent.sdk_schema.subprocess.run", fake_run)
    out = fetch_sdk("google-genai", "0.0.999", cache_root=tmp_path)
    assert out is not None
    assert len(calls) == 2
    assert any("==" in a for a in calls[0])
    assert not any("==" in a for a in calls[1])


def test_fetch_sdk_cache_hit_skips_pip(tmp_path, monkeypatch):
    hit = tmp_path / "google-genai-1.2.0"
    (hit / "pkg").mkdir(parents=True)

    def boom(*a, **k):
        raise AssertionError("pip must not run on cache hit")

    monkeypatch.setattr("tvastr.agent.sdk_schema.subprocess.run", boom)
    out = fetch_sdk("google-genai", "1.2.0", cache_root=tmp_path)
    assert out == hit


def test_fetch_sdk_pip_failure_returns_none(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        class R:
            returncode = 1
            stderr = b"network down"
        return R()

    monkeypatch.setattr("tvastr.agent.sdk_schema.subprocess.run", fake_run)
    assert fetch_sdk("google-genai", None, cache_root=tmp_path) is None


# --- prompt + block formatting ---

def test_build_probe_prompt_contains_inputs():
    p = build_probe_prompt("t", "diag", ["a.py"], "body words")
    assert "diag" in p and "a.py" in p and "body words" in p
    assert "JSON" in p


def test_format_schema_block_labels_package_and_paths():
    block = format_schema_block(
        "google-genai", [Snippet(path="google/genai/types.py", text="class X:\n    f: int")]
    )
    assert "google-genai" in block
    assert "google/genai/types.py" in block
    assert "class X:" in block
    assert "ground truth" in block.lower()


def test_format_schema_block_empty_returns_empty():
    assert format_schema_block("p", []) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_sdk_schema.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'tvastr.agent.sdk_schema'`

- [ ] **Step 3: Implement the module**

Create `src/tvastr/agent/sdk_schema.py`:

```python
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
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

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
                lines = lines[:max_lines_each] + ["    ..."]
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_sdk_schema.py -q`
Expected: all PASS.

- [ ] **Step 5: Lint and full suite**

Run: `uv run ruff check src tests && uv run pytest -q 2>&1 | tail -1`
Expected: `All checks passed!` and the suite green (309 passed + new, 1 skipped).

- [ ] **Step 6: Commit**

```bash
git add src/tvastr/agent/sdk_schema.py tests/test_sdk_schema.py
git commit -m "feat(agent): SDK-schema primitives — probe parse, wheels-only fetch, class-block extraction"
```

---

### Task 2: Wire SDK-schema evidence into `_ground_root_cause`

**Files:**
- Modify: `src/tvastr/llm/router.py` (TaskType, line ~44)
- Modify: `src/tvastr/config.py` (flag, after `issue_era_retrieval` ~line 51)
- Modify: `src/tvastr/agent/context.py` (field, after `issue_era_retrieval` ~line 67)
- Modify: `src/tvastr/pipeline.py` (ctx construction ~line 270)
- Modify: `src/tvastr/agent/graph.py` (`_ground_root_cause` + new `_sdk_schema_evidence` helper)
- Modify: `tests/conftest.py` (seal, after line 20)
- Modify: `.gitignore` (after `data/runs/`)
- Test: `tests/test_agent_grounding.py` (append wiring tests; follow that file's existing fake/router conventions)

**Interfaces:**
- Consumes from Task 1 (exact): `build_probe_prompt`, `PROBE_SYSTEM`, `parse_probe`, `fetch_sdk`, `extract_schema_snippets`, `format_schema_block`.
- Produces: `TaskType.SCHEMA_PROBE`; `Settings.sdk_schema_grounding: bool = True`; `AgentContext.sdk_schema_grounding: bool = False`; graph helper `_sdk_schema_evidence(pattern, root_cause, state) -> str` (returns the labeled block or `""`); event `doc.sdk_schema` `{package, version, ok, reason?, snippets, files}`.

- [ ] **Step 1: Write the failing wiring tests**

Append to `tests/test_agent_grounding.py` (reuse that file's existing helpers for building an agent with a scripted router; if it lacks one, mirror `_agent`/`_SeqRouter`/`_FakeHost`/`_pattern` from `tests/test_agent_investigate.py`, and construct the ctx with `doc_grounding=True, sdk_schema_grounding=True`):

```python
# --- SDK-schema grounding wiring ---

_PROBE_YES = (
    '{"relevant": true, "package": "google-genai", "version_hint": null,'
    ' "keywords": ["usage_metadata"]}'
)


def _grounding_state():
    from tvastr.domain import RootCause
    rc = RootCause(pattern_id="p", summary="tokens not mapped",
                   suspected_files=["utils.py"], confidence=0.6)
    return {"pattern": _pattern(), "root_cause": rc,
            "code_context": "(ctx)", "issue_body": "body"}


def _snippet_dir(tmp_path):
    pkg = tmp_path / "google-genai-latest"
    (pkg / "google").mkdir(parents=True)
    (pkg / "google" / "types.py").write_text(
        "class UsageMetadata:\n    usage_metadata: int\n"
        "    response_token_count: int\n"
    )
    return pkg


def test_grounding_prompt_enriched_with_sdk_schema(tmp_path, monkeypatch):
    prompts = []

    class _CapRouter(_SeqRouter):
        def run(self, task, prompt, *, sensitivity=None, system=None, web_search=False):
            prompts.append((task.value, prompt))
            return super().run(task, prompt, sensitivity=sensitivity, system=system)

    router = _CapRouter([_PROBE_YES, "corrected diagnosis text"])
    monkeypatch.setattr(
        "tvastr.agent.graph.fetch_sdk", lambda pkg, ver: _snippet_dir(tmp_path)
    )
    agent = _agent_with_grounding(router, sdk_schema=True)
    out = agent._ground_root_cause(_grounding_state())
    tasks = [t for t, _ in prompts]
    assert tasks == ["schema_probe", "doc_grounding"]
    grounding_prompt = prompts[1][1]
    assert "response_token_count" in grounding_prompt
    assert "ground truth" in grounding_prompt.lower()
    assert out["root_cause"].summary == "corrected diagnosis text"


def test_probe_not_relevant_leaves_prompt_unchanged(monkeypatch):
    prompts = []

    class _CapRouter(_SeqRouter):
        def run(self, task, prompt, *, sensitivity=None, system=None, web_search=False):
            prompts.append((task.value, prompt))
            return super().run(task, prompt, sensitivity=sensitivity, system=system)

    router = _CapRouter(['{"relevant": false}', "grounded"])
    monkeypatch.setattr(
        "tvastr.agent.graph.fetch_sdk",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )
    agent = _agent_with_grounding(router, sdk_schema=True)
    agent._ground_root_cause(_grounding_state())
    grounding_prompt = prompts[1][1]
    assert "ground truth" not in grounding_prompt.lower()


def test_fetch_failure_degrades_and_emits_skip_event(monkeypatch):
    from tvastr.events import ListEventSink
    sink = ListEventSink()
    router = _SeqRouter([_PROBE_YES, "grounded"])
    monkeypatch.setattr("tvastr.agent.graph.fetch_sdk", lambda *a, **k: None)
    agent = _agent_with_grounding(router, sdk_schema=True, sink=sink)
    out = agent._ground_root_cause(_grounding_state())
    assert out["root_cause"].summary == "grounded"  # grounding still ran
    ev = [e for e in sink.events if e.type == "doc.sdk_schema"]
    assert len(ev) == 1
    assert ev[0].payload["ok"] is False
    assert "fetch" in ev[0].payload["reason"]


def test_sdk_schema_success_emits_event(tmp_path, monkeypatch):
    from tvastr.events import ListEventSink
    sink = ListEventSink()
    router = _SeqRouter([_PROBE_YES, "grounded"])
    monkeypatch.setattr(
        "tvastr.agent.graph.fetch_sdk", lambda pkg, ver: _snippet_dir(tmp_path)
    )
    agent = _agent_with_grounding(router, sdk_schema=True, sink=sink)
    agent._ground_root_cause(_grounding_state())
    ev = [e for e in sink.events if e.type == "doc.sdk_schema"]
    assert len(ev) == 1
    p = ev[0].payload
    assert p["ok"] is True and p["package"] == "google-genai" and p["snippets"] == 1


def test_flag_off_makes_zero_probe_calls():
    router = _SeqRouter(["grounded only"])
    agent = _agent_with_grounding(router, sdk_schema=False)
    agent._ground_root_cause(_grounding_state())
    assert router.calls == 1  # doc_grounding only


def test_probe_exception_degrades(monkeypatch):
    class _BoomFirstRouter(_SeqRouter):
        def run(self, task, prompt, *, sensitivity=None, system=None, web_search=False):
            if task.value == "schema_probe":
                raise RuntimeError("llm down")
            return super().run(task, prompt, sensitivity=sensitivity, system=system)

    router = _BoomFirstRouter(["grounded"])
    agent = _agent_with_grounding(router, sdk_schema=True)
    out = agent._ground_root_cause(_grounding_state())
    assert out["root_cause"].summary == "grounded"
```

Also add the tiny helper the tests above use (adapt to the file's local conventions):

```python
def _agent_with_grounding(router, *, sdk_schema, sink=None):
    agent = _agent(_FakeHost(), router, sink)
    agent.ctx.doc_grounding = True
    agent.ctx.sdk_schema_grounding = sdk_schema
    return agent
```

(No standalone `Settings` assertion test — this repo tests flags behaviorally, and `test_flag_off_makes_zero_probe_calls` covers the off-path.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_agent_grounding.py -q`
Expected: FAIL — `ImportError`/`AttributeError` (`sdk_schema_grounding`, `TaskType.SCHEMA_PROBE`, `tvastr.agent.graph.fetch_sdk` missing).

- [ ] **Step 3: Implement the plumbing**

(a) `src/tvastr/llm/router.py` — after `REPRO_CRITIQUE = "repro_critique"` add:

```python
    SCHEMA_PROBE = "schema_probe"
```

(b) `src/tvastr/config.py` — after the `issue_era_retrieval` field add:

```python
    # When true, doc-grounding may install the diagnosis-relevant third-party
    # SDK (host pip, wheels-only, no-deps, into data/sdk_cache/) and inject its
    # type definitions into the grounding prompt as ground truth for field
    # names. Off in tests.
    sdk_schema_grounding: bool = True
```

(c) `src/tvastr/agent/context.py` — after `issue_era_retrieval: bool = False` add:

```python
    sdk_schema_grounding: bool = False
```

(d) `src/tvastr/pipeline.py` — in the `AgentContext(...)` construction, after the `issue_era_retrieval=settings.issue_era_retrieval,` line add:

```python
        sdk_schema_grounding=settings.sdk_schema_grounding,
```

(e) `tests/conftest.py` — after the `TVASTR_ISSUE_ERA_RETRIEVAL` line add:

```python
os.environ["TVASTR_SDK_SCHEMA_GROUNDING"] = "false"
```

(f) `.gitignore` — after `data/runs/` add:

```
data/sdk_cache/
```

(g) `src/tvastr/agent/graph.py` — add imports (with the other `tvastr.agent.*` imports):

```python
from tvastr.agent.sdk_schema import (
    PROBE_SYSTEM,
    build_probe_prompt,
    extract_schema_snippets,
    fetch_sdk,
    format_schema_block,
    parse_probe,
)
```

In `_ground_root_cause`, immediately after the `self._emit("agent.node.start", ...)` line (graph.py:325) and before the `prompt = (` line, insert:

```python
        schema_block = ""
        if self.ctx.sdk_schema_grounding:
            schema_block = self._sdk_schema_evidence(pattern, root_cause, state)
```

Then change the prompt construction to include the block (replace the existing `prompt = (...)` statement):

```python
        prompt = (
            f"Failure: {pattern.title}\n"
            f"Current diagnosis: {root_cause.summary}\n\n"
            + (f"{schema_block}\n\n" if schema_block else "")
            + f"Code context:\n{state.get('code_context') or '(none)'}\n\n"
            "Validate this diagnosis against authoritative external documentation. "
            "Use web_search ONLY if the root cause depends on third-party API/library "
            "behavior (e.g. a renamed field or changed return shape in a dependency). "
            "Return ONLY the corrected root-cause summary in 2-4 sentences; if the "
            "original was correct, restate it concisely."
        )
```

Add the helper method after `_ground_root_cause` (before `_generate_fix`):

```python
    def _sdk_schema_evidence(
        self, pattern: FailurePattern, root_cause: RootCause, state: AgentState
    ) -> str:
        """Probe → fetch → extract → format. Returns "" on every failure rung.

        Emits ``doc.sdk_schema`` in all paths so the timeline shows whether the
        grounding prompt carried real SDK definitions.
        """

        def _skip(reason: str, **extra: object) -> str:
            self._emit("doc.sdk_schema", "ground_root_cause",
                       ok=False, reason=reason, snippets=0, files=[], **extra)
            return ""

        try:
            response, decision = self.ctx.router.run(
                TaskType.SCHEMA_PROBE,
                build_probe_prompt(
                    pattern.title,
                    root_cause.summary,
                    list(root_cause.suspected_files),
                    state.get("issue_body") or "",
                ),
                sensitivity=pattern.sensitivity,
                system=PROBE_SYSTEM,
            )
        except Exception as exc:
            log.warning("agent.sdk_schema.probe_failed", error=str(exc))
            return _skip(f"probe error: {exc}")
        probe = parse_probe(response.text)
        if probe is None:
            return _skip("probe: not relevant or unparseable")
        root = fetch_sdk(probe.package, probe.version_hint)
        if root is None:
            return _skip(f"fetch failed: {probe.package}", package=probe.package)
        snippets = extract_schema_snippets(root, probe.keywords)
        if not snippets:
            return _skip("no schema matches", package=probe.package)
        self._emit(
            "doc.sdk_schema", "ground_root_cause",
            ok=True, package=probe.package,
            version=probe.version_hint or "latest",
            snippets=len(snippets), files=[s.path for s in snippets],
        )
        return format_schema_block(probe.package, snippets)
```

Note: `_skip` passes `**extra` so the fetch-failure rung still records the package; duplicate-keyword collisions are avoided because `package` is only in `extra` on rungs that don't already pass it.

- [ ] **Step 4: Run the wiring tests, full suite, lint**

Run: `uv run pytest tests/test_agent_grounding.py tests/test_sdk_schema.py -q && uv run pytest -q 2>&1 | tail -1 && uv run ruff check src tests`
Expected: all green, lint clean. (The conftest seal keeps every pre-existing test on today's behavior.)

- [ ] **Step 5: Commit**

```bash
git add src/tvastr/llm/router.py src/tvastr/config.py src/tvastr/agent/context.py src/tvastr/pipeline.py src/tvastr/agent/graph.py tests/conftest.py tests/test_agent_grounding.py .gitignore
git commit -m "feat(agent): SDK-schema grounding — probe/fetch/extract wired into ground_root_cause"
```

---

### Task 3: Dashboard `doc.sdk_schema` rendering + offline verification

**Files:**
- Modify: `src/tvastr/api/templates/app.html` (`summarize(event)` switch, next to the `doc.grounded` case)

**Interfaces:**
- Consumes: the `doc.sdk_schema` payload from Task 2: `{package, version, ok, reason?, snippets, files}`.

- [ ] **Step 1: Add the summarize case**

In `summarize(event)`, directly after the `case "doc.skipped":` line, insert:

```js
    case "doc.sdk_schema": return p.ok
      ? `${p.package}@${p.version} · ${p.snippets} snippet(s) from installed SDK`
      : `skipped — ${p.reason || ""}`;
```

- [ ] **Step 2: Verify the served page and suite**

Run:
```bash
curl -s -o /dev/null -w "GET /app -> %{http_code}\n" http://localhost:8000/app
curl -s http://localhost:8000/app | grep -c "doc.sdk_schema"
uv run pytest -q 2>&1 | tail -1 && uv run ruff check src tests
```
Expected: `GET /app -> 200`; grep count ≥ `1`; suite green; lint clean.

- [ ] **Step 3: Structural sanity of the probe path against #19293's persisted diagnosis (no network, no LLM)**

Run:
```bash
uv run python - <<'EOF'
from pathlib import Path
from tvastr.agent.sdk_schema import build_probe_prompt, parse_probe, extract_schema_snippets

# parse_probe on the exact JSON shape the probe is asked for
probe = parse_probe('{"relevant": true, "package": "google-genai", '
                    '"version_hint": null, "keywords": ["usage_metadata", '
                    '"candidates_token_count"]}')
assert probe and probe.package == "google-genai"
# extraction against a fabricated google-genai usage class
root = Path("/tmp/sdk-sanity"); pkg = root / "google" / "genai"
pkg.mkdir(parents=True, exist_ok=True)
(pkg / "types.py").write_text(
    "class GenerateContentResponseUsageMetadata:\n"
    "    prompt_token_count: int | None = None\n"
    "    candidates_token_count: int | None = None\n"
    "    response_token_count: int | None = None\n")
snips = extract_schema_snippets(root, probe.keywords)
assert snips and "response_token_count" in snips[0].text
print("probe→extract path OK; rename field would reach the grounding prompt")
EOF
```
Expected: `probe→extract path OK; rename field would reach the grounding prompt`

- [ ] **Step 4: Commit**

```bash
git add src/tvastr/api/templates/app.html
git commit -m "feat(ui): render doc.sdk_schema in the pipeline timeline"
```

**Live validation (controller + user, post-merge, not this task):** re-run #19293; expect `doc.sdk_schema ok:true package=google-genai`, a corrected field-rename diagnosis, and `benchmark.compared` moving off `divergent`.
