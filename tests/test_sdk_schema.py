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


def test_parse_probe_rejects_path_traversal_version_hint():
    # version_hint is LLM-controlled and feeds a pip --target path; a
    # traversal-shaped hint must be dropped, not passed through, but the
    # probe itself is still usable (best-effort advisory field).
    for bad in ("/../../../tmp/pwned", "1.2.0/../x", ".."):
        text = (
            '{"relevant": true, "package": "p", "version_hint": "'
            f'{bad}", "keywords": ["k"]}}'
        )
        probe = parse_probe(text)
        assert probe is not None, bad
        assert probe.version_hint is None, bad


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


def test_extract_handles_multiline_class_header(tmp_path):
    # A black-formatted multi-line class header (`class Long(\n    Base,\n):`)
    # has no ':' on its `class` line, so a header regex requiring one misses
    # the boundary and the class's body gets absorbed into the previous
    # class's block (wrong snippet attribution).
    root = tmp_path / "r"
    root.mkdir()
    (root / "m.py").write_text(
        "class First:\n"
        "    only_in_first = 1\n\n\n"
        "class Long(\n"
        "    Base,\n"
        "):\n"
        "    only_in_long = 2\n"
    )
    snips = extract_schema_snippets(root, ["only_in_long"])
    assert len(snips) == 1
    assert snips[0].text.startswith("class Long(")
    assert "only_in_first" not in snips[0].text

    snips_first = extract_schema_snippets(root, ["only_in_first"])
    assert len(snips_first) == 1
    assert snips_first[0].text.startswith("class First:")
    assert "only_in_long" not in snips_first[0].text


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


def test_fetch_sdk_rejects_path_traversal_version_hint(tmp_path, monkeypatch):
    # An unvalidated version_hint (e.g. from parse_probe callers that don't
    # go through parse_probe) feeds directly into the --target path; a
    # traversal-shaped hint must not escape cache_root and must not be
    # passed to pip as a version pin.
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        t = Path(argv[argv.index("--target") + 1])
        t.mkdir(parents=True, exist_ok=True)
        (t / "pkg").mkdir(exist_ok=True)

        class R:
            returncode = 0
            stderr = b""
        return R()

    monkeypatch.setattr("tvastr.agent.sdk_schema.subprocess.run", fake_run)
    out = fetch_sdk("google-genai", "/../../../tmp/pwned", cache_root=tmp_path)
    assert out is not None
    assert out.resolve().is_relative_to(tmp_path.resolve())
    assert not any("==" in a for a in calls[0])


def test_fetch_sdk_purges_partial_cache_on_failure(tmp_path, monkeypatch):
    # A failed/timed-out pip attempt can leave a partially-written target
    # dir; the cache check (`is_dir() and any(iterdir())`) must not treat
    # that as a permanent false cache hit.
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        t = Path(argv[argv.index("--target") + 1])
        t.mkdir(parents=True, exist_ok=True)
        (t / "partial.txt").write_text("junk")

        class R:
            returncode = 1
            stderr = b"boom"
        return R()

    monkeypatch.setattr("tvastr.agent.sdk_schema.subprocess.run", fake_run)
    target = tmp_path / "google-genai-1.2.0"
    out = fetch_sdk("google-genai", "1.2.0", cache_root=tmp_path)
    assert out is None
    assert len(calls) == 2  # pinned attempt + unpinned retry, both failed
    assert not target.exists()

    calls.clear()
    fetch_sdk("google-genai", "1.2.0", cache_root=tmp_path)
    assert len(calls) == 2  # re-invoked pip; no false cache hit from partial dir


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
