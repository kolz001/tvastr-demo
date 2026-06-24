from __future__ import annotations

import json

from tvastr.analysis.fix_comparison import FixComparison, compare_fix_to_pr
from tvastr.analysis.pr_discovery import PrDiff, PrFile, PullRequestRef
from tvastr.domain import FileChange, FixProposal, RoutingDecision, Sensitivity
from tvastr.llm.base import LLMResponse


class StubRouter:
    def __init__(self, text):
        self._text = text
        self.last_prompt = None

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        self.last_prompt = prompt
        d = RoutingDecision(
            task=task.value,
            target="cloud",
            model="stub",
            sensitivity=sensitivity,
            reason="stub",
        )
        return (
            LLMResponse(text=self._text, model="stub", target="cloud", mocked=True),
            d,
        )


def _fix(paths):
    return FixProposal(
        pattern_id="p",
        summary="our fix",
        changes=[FileChange(path=p, patched_content="x") for p in paths],
    )


def _pr_diff(paths):
    return PrDiff(files=[PrFile(p, "modified", 1, 0, "@@\n+x") for p in paths])


def _ref():
    return PullRequestRef(30, "fix", "open", False, "u", 1)


def test_file_overlap_is_computed_in_code():
    router = StubRouter(
        json.dumps(
            {
                "verdict": "match",
                "same_root_cause": True,
                "equivalence": "functionally_equivalent",
                "rationale": "same change",
                "confidence": 0.9,
            }
        )
    )
    cmp, _ = compare_fix_to_pr(
        "t", "rc", _fix(["a.py", "b.py"]), _ref(), _pr_diff(["a.py", "c.py"]), router
    )
    assert isinstance(cmp, FixComparison)
    assert cmp.files_both == ["a.py"]
    assert cmp.files_ours_only == ["b.py"]
    assert cmp.files_theirs_only == ["c.py"]
    assert cmp.verdict == "match"
    assert cmp.same_root_cause is True


def test_unparseable_response_degrades_to_divergent_low_confidence():
    cmp, _ = compare_fix_to_pr(
        "t", "rc", _fix(["a.py"]), _ref(), _pr_diff(["b.py"]), StubRouter("garbage")
    )
    assert cmp.verdict == "divergent"
    assert cmp.confidence == 0.0
    assert cmp.files_both == []


def test_comparison_parses_json_wrapped_in_prose():
    payload = (
        'Analysis: {"verdict": "partial", "same_root_cause": true, '
        '"equivalence": "same_goal_different_approach", "rationale": "r", '
        '"confidence": 0.7} done.'
    )
    cmp, _ = compare_fix_to_pr(
        "t", "rc", _fix(["a.py"]), _ref(), _pr_diff(["a.py"]), StubRouter(payload)
    )
    assert cmp.verdict == "partial"
    assert cmp.confidence == 0.7
