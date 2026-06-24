# Plan: Hybrid local PII detection (regex floor + Presidio NER)

Branch: `feature/local-pii-presidio` (off main)

## Goal
Augment the conservative regex redactor with a **local** Presidio (spaCy NER)
layer so unstructured PII (names, locations, orgs) is caught alongside the
structured secrets regex already covers — without weakening the deterministic
floor and without changing the public interface or the default install.

## Hard constraints (non-negotiable)
- **Interface unchanged:** `redact(text) -> (str, list[str])` and
  `contains_pii(text) -> bool` keep their exact signatures. The two callers
  (`llm/router.py:88`, `detection/detector.py:33`) are not touched.
- **Local only:** Presidio/spaCy run on-device. This layer sits *before* the
  cloud boundary, so it must never itself make a network call. (This is the
  whole point — detecting PII must not leak PII.)
- **Regex is the floor, model is additive:** on any overlap the regex (more
  specific, deterministic) wins. The model can only *add* redactions, never
  remove or downgrade one.
- **Fail-open to regex:** if the `pii` extra isn't installed, the model fails
  to load, or analysis raises — log once and fall back to regex-only. We never
  end up redacting *less* than today.
- **Default behavior identical:** flag defaults off; with it off and/or the
  extra absent, output is byte-identical to today. Existing `tests/test_pii.py`
  pass unchanged.

## Design

### 1. Span-based redaction core (refactor `redaction.py` internals)
Today `redact()` applies each pattern's `.sub()` sequentially over the
progressively-redacted text. To union regex + model hits cleanly, switch to a
span model — but preserve identical output for the regex-only path:
- `Span(start, end, label, source)` — offsets into the **original** text.
- Collect regex spans by scanning `_PATTERNS` in order on the original text.
- Collect model spans (if enabled) on the original text.
- **Merge with precedence:** regex beats model; within regex, earlier pattern
  in `_PATTERNS` wins (preserves the documented CREDENTIAL_URL-before-EMAIL
  rule). Drop any span that overlaps an already-accepted higher-precedence one.
- Apply replacements by offset, **descending**, so earlier offsets stay valid.
- `labels_found` = accepted spans' labels, deduped in detection order
  (regex labels first, then model labels) — keeps current ordering for the
  regex-only case.

Risk: output equivalence with the sequential approach. Mitigation: the existing
`tests/test_pii.py` is the oracle — it must stay green with zero edits. TDD:
land the span refactor first (regex-only), prove the suite is unchanged, *then*
add the model layer.

### 2. `src/tvastr/pii/_presidio.py` (new) — the local NER seam
- `model_spans(text) -> list[Span]`: returns `[]` immediately unless enabled.
- `_enabled()`: reads `get_settings().pii_local_model` (cached); memoized.
- `_get_analyzer()`: lazily builds a singleton Presidio `AnalyzerEngine`
  (default spaCy backend). Soft import: on `ImportError` / load failure, log
  once and return `None` → `model_spans` returns `[]` (fail-open).
- Label map: Presidio entity → our label (`PERSON`→PERSON, `LOCATION`→LOCATION,
  `NRP`/`ORGANIZATION`→ORG, etc.). Entities we already cover deterministically
  (EMAIL_ADDRESS, IP_ADDRESS) are *excluded* from the model set so regex stays
  the source of truth for those.
- A confidence threshold (default ~0.5) filters weak spaCy hits.

### 3. `config.py` — one flag
`pii_local_model: bool = False` on `Settings`. Off by default so the standard
install/tests are unchanged; flip it on (with `tvastr[pii]` installed) for the
hybrid.

### 4. `pyproject.toml` — optional extra
```toml
[project.optional-dependencies]
pii = ["presidio-analyzer>=2.2", "presidio-anonymizer>=2.2", "spacy>=3.7"]
```
The spaCy *model* is a runtime download, not a pip dep — document
`python -m spacy download en_core_web_lg` in the module docstring + README.

### 5. Tests (`tests/test_pii.py`)
- **Unchanged:** every existing regex test stays green (proves equivalence + default-off).
- **New (no heavy dep needed — stub the analyzer seam):**
  - flag on + stub `model_spans` returning a PERSON span → name gets `[REDACTED:PERSON]`, unioned with regex hits in the same text.
  - overlap: model claims a span that regex also matches (e.g. an email) → **regex label wins**, no double-redaction.
  - fail-open: `_get_analyzer` raises → `redact` returns regex-only result, no exception.
  - flag off → `model_spans` not consulted; output identical to regex-only.
- Optional: one real-Presidio integration test guarded by
  `pytest.importorskip("presidio_analyzer")` so it runs only where the extra is installed.

## Files
| File | Change |
|------|--------|
| `src/tvastr/pii/redaction.py` | refactor to span-based union; call `model_spans`; interface unchanged |
| `src/tvastr/pii/_presidio.py` | NEW — lazy analyzer, `model_spans`, label map, fail-open |
| `src/tvastr/config.py` | add `pii_local_model: bool = False` |
| `pyproject.toml` | add `pii` optional extra |
| `tests/test_pii.py` | keep existing; add hybrid/fail-open/overlap tests via stub |
| `README.md` / module docstring | document the extra + spaCy model download |

## Sequencing (TDD)
1. Span refactor of `redact()` (regex-only) → existing suite green, no edits to it.
2. `_presidio.py` seam + config flag + `model_spans` wiring → stubbed-analyzer tests.
3. `pyproject` extra + docs.
4. Lint + full suite + commit.

## Out of scope (deferred)
- GLiNER / custom domain recognizers (internal hostnames, service names) —
  revisit once the spaCy baseline is in.
- Anonymization beyond our `[REDACTED:LABEL]` markers (Presidio's anonymizer
  operators) — our marker scheme is the contract callers expect.
