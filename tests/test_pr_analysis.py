from __future__ import annotations

import json

from tvastr.analysis.pr_analysis import PrAnalysis, analyze_pr
from tvastr.analysis.pr_discovery import PrDiff, PrFile, PullRequestRef
from tvastr.domain import RoutingDecision, Sensitivity
from tvastr.llm.base import LLMResponse


class StubRouter:
    def __init__(self, text):
        self._text = text
        self.last_prompt = None

    def run(self, task, prompt, *, sensitivity=Sensitivity.INTERNAL, system=None):
        self.last_prompt = prompt
        decision = RoutingDecision(
            task=task.value, target="cloud", model="stub", sensitivity=sensitivity, reason="stub"
        )
        return LLMResponse(text=self._text, model="stub", target="cloud", mocked=True), decision


def _ref():
    return PullRequestRef(
        30, "fix: token counting", "open", False, "https://github.com/o/r/pull/30", 2
    )


def _diff():
    return PrDiff(files=[PrFile("llama_index/core/llms.py", "modified", 5, 1, "@@\n+fix")])


def test_analyze_pr_parses_structured_verdict():
    payload = json.dumps(
        {
            "addresses_issue": "yes",
            "approach_summary": "Adds usage extraction for Gemini.",
            "key_files": ["llama_index/core/llms.py"],
            "root_cause": "Token usage not parsed from the response.",
        }
    )
    router = StubRouter(payload)
    analysis, decision = analyze_pr("title", "body", _ref(), _diff(), router)
    assert isinstance(analysis, PrAnalysis)
    assert analysis.addresses_issue == "yes"
    assert analysis.key_files == ["llama_index/core/llms.py"]
    assert analysis.pr_number == 30
    assert decision.task == "pr_analysis"
    assert "fix" in router.last_prompt  # diff reached the prompt


def test_analyze_pr_tolerates_unparseable_response():
    analysis, _ = analyze_pr("t", "b", _ref(), _diff(), StubRouter("not json"))
    assert analysis.addresses_issue == "unknown"
    assert analysis.pr_number == 30
