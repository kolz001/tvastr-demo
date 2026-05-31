from dataclasses import dataclass

from tvastr.agent.context import AgentContext
from tvastr.agent.tools.fix_generation import (
    _apply_change,
    _build_real_changes,
    _extract_json,
    _parse_response,
    generate_fix,
)
from tvastr.config import Settings
from tvastr.domain import FailurePattern, RootCause, Sensitivity
from tvastr.integrations import build_notifier
from tvastr.integrations.github import MockGitHubClient
from tvastr.llm.base import LLMResponse
from tvastr.llm.router import build_router

# --- _apply_change ---------------------------------------------------------


def test_apply_change_replaces_unique_match() -> None:
    content = "line one\nline two\nline three\n"
    new, err = _apply_change(content, "line two", "LINE TWO")
    assert err == ""
    assert new == "line one\nLINE TWO\nline three\n"


def test_apply_change_rejects_missing_search() -> None:
    new, err = _apply_change("hello world", "nope", "x")
    assert new is None
    assert "not found" in err


def test_apply_change_rejects_ambiguous_search() -> None:
    content = "foo\nfoo\n"
    new, err = _apply_change(content, "foo", "bar")
    assert new is None
    assert "appears 2 times" in err


# --- JSON parsing ----------------------------------------------------------


def test_extract_json_handles_bare_object() -> None:
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_strips_markdown_fences() -> None:
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_recovers_from_leading_prose() -> None:
    assert _extract_json('Sure! Here it is:\n{"a": 1}\nLet me know!') == {"a": 1}


def test_extract_json_returns_none_for_garbage() -> None:
    assert _extract_json("no json here at all") is None


def test_parse_response_filters_malformed_changes() -> None:
    raw = (
        '{"summary": "s", "test_plan": "t", "changes": ['
        '{"path": "a.py", "search": "x", "replace": "y", "rationale": "r"},'
        '{"path": "b.py", "search": 123, "replace": "y"},'  # bad type → dropped
        '"not a dict"'  # dropped
        "]}"
    )
    parsed = _parse_response(raw)
    assert parsed is not None
    assert len(parsed.changes) == 1
    assert parsed.changes[0]["path"] == "a.py"


# --- _build_real_changes ---------------------------------------------------


def test_build_real_changes_applies_multiple_ops_per_file() -> None:
    from tvastr.agent.tools.fix_generation import _ParsedFix

    parsed = _ParsedFix(
        summary="two hunks",
        changes=[
            {"path": "a.py", "search": "alpha", "replace": "ALPHA", "rationale": "first"},
            {"path": "a.py", "search": "beta", "replace": "BETA", "rationale": "second"},
        ],
        test_plan="",
    )
    files = {"a.py": "alpha\nbeta\ngamma\n"}
    changes, errors = _build_real_changes(parsed, files)
    assert errors == []
    assert len(changes) == 1
    assert changes[0].patched_content == "ALPHA\nBETA\ngamma\n"
    assert "first" in changes[0].rationale and "second" in changes[0].rationale


def test_build_real_changes_collects_errors_for_unknown_path() -> None:
    from tvastr.agent.tools.fix_generation import _ParsedFix

    parsed = _ParsedFix(
        summary="",
        changes=[
            {"path": "ghost.py", "search": "x", "replace": "y", "rationale": ""},
        ],
        test_plan="",
    )
    changes, errors = _build_real_changes(parsed, {"real.py": "x\n"})
    assert changes == []
    assert any("ghost.py" in e for e in errors)


def test_build_real_changes_skips_files_with_no_net_change() -> None:
    from tvastr.agent.tools.fix_generation import _ParsedFix

    parsed = _ParsedFix(
        summary="",
        changes=[{"path": "a.py", "search": "x", "replace": "x", "rationale": ""}],
        test_plan="",
    )
    changes, errors = _build_real_changes(parsed, {"a.py": "x\n"})
    assert changes == []  # nothing actually changed
    assert errors == []


# --- generate_fix end-to-end (real router + scripted LLM) ------------------


@dataclass
class _ScriptedLLM:
    response_text: str
    model: str = "claude-opus-4-7"
    target: str = "cloud"

    def complete(self, prompt: str, *, system: str | None = None) -> LLMResponse:
        return LLMResponse(
            text=self.response_text, model=self.model, target=self.target, mocked=True
        )


def _ctx_with_llm(llm: _ScriptedLLM) -> AgentContext:
    settings = Settings(use_mocks=True, audit_backend="memory")
    router = build_router(settings)
    router.cloud = llm
    return AgentContext(
        router=router, code_host=MockGitHubClient(), notifier=build_notifier(settings)
    )


def _pattern() -> FailurePattern:
    return FailurePattern(
        fingerprint="abc123",
        title="ValueError in service",
        representative_message="ValueError: bad input",
        exception_type="ValueError",
        count=3,
        sensitivity=Sensitivity.INTERNAL,
    )


def _root_cause(pattern: FailurePattern) -> RootCause:
    return RootCause(
        pattern_id=pattern.id,
        summary="The function does not validate its input.",
        suspected_files=["mod.py"],
        confidence=0.8,
    )


def test_generate_fix_applies_valid_json_response() -> None:
    response = (
        '{"summary": "Guard against None.", "test_plan": "Add test for None.",'
        ' "changes": [{"path": "mod.py", "search": "return x + 1",'
        ' "replace": "if x is None:\\n    return 0\\nreturn x + 1",'
        ' "rationale": "handle None input"}]}'
    )
    ctx = _ctx_with_llm(_ScriptedLLM(response))
    pattern = _pattern()
    fix, _ = generate_fix(ctx, pattern, _root_cause(pattern), {"mod.py": "return x + 1\n"})

    assert len(fix.changes) == 1
    assert fix.changes[0].path == "mod.py"
    assert "if x is None" in fix.changes[0].patched_content
    assert fix.summary == "Guard against None."
    assert "Add test for None." in fix.test_plan


def test_generate_fix_falls_back_when_response_is_unparseable() -> None:
    ctx = _ctx_with_llm(_ScriptedLLM("I refuse to produce JSON, sorry."))
    pattern = _pattern()
    fix, _ = generate_fix(ctx, pattern, _root_cause(pattern), {"mod.py": "return x\n"})

    # Fallback still emits a FileChange so the rest of the pipeline can run.
    assert len(fix.changes) == 1
    assert fix.changes[0].path == "mod.py"
    assert "could not be applied automatically" in fix.changes[0].patched_content
    assert "could not be parsed/validated" in fix.changes[0].rationale


def test_generate_fix_falls_back_when_search_strings_are_invalid() -> None:
    response = (
        '{"summary": "x", "test_plan": "y", "changes": ['
        '{"path": "mod.py", "search": "this text is not in the file",'
        ' "replace": "anything", "rationale": "r"}]}'
    )
    ctx = _ctx_with_llm(_ScriptedLLM(response))
    pattern = _pattern()
    fix, _ = generate_fix(ctx, pattern, _root_cause(pattern), {"mod.py": "actual content\n"})

    assert len(fix.changes) == 1
    assert "could not be applied automatically" in fix.changes[0].patched_content


def test_generate_fix_with_mock_claude_produces_applicable_change() -> None:
    """The MockClaudeClient should return JSON that actually validates and applies."""
    settings = Settings(use_mocks=True, audit_backend="memory")
    router = build_router(settings)
    ctx = AgentContext(
        router=router,
        code_host=MockGitHubClient(),
        notifier=build_notifier(settings),
    )
    pattern = _pattern()
    files = {"llama_index/llms/openai/foo.py": "def run(self):\n    return None\n"}
    fix, _ = generate_fix(ctx, pattern, _root_cause(pattern), files)

    assert len(fix.changes) == 1
    assert "tvastr-mock" in fix.changes[0].patched_content
    # The mock's marker should NOT appear in the original file.
    assert "tvastr-mock" not in files["llama_index/llms/openai/foo.py"]
