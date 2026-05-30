"""The remediation agent as a LangGraph state machine.

Flow::

    START -> investigate -> reason_root_cause -> (confidence gate)
        high → generate_fix → draft_pr → open_pr → notify → END
        low  → notify (skipped) → END

The confidence gate is the ReAct-style decision point: the agent only acts (opens a
PR) when its root-cause analysis is confident enough; otherwise it escalates to a
human via notification. Runs end-to-end offline when the context holds mock backends.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from tvastr.agent.context import AgentContext
from tvastr.agent.state import AgentState
from tvastr.agent.tools import (
    extract_stack_files,
    generate_fix,
    open_pull_request,
    retrieve_code,
    search_codebase,
    send_notification,
)
from tvastr.domain import PullRequestDraft, RootCause, RoutingDecision
from tvastr.llm.router import TaskType
from tvastr.logging import get_logger

log = get_logger(__name__)


def _append_routing(state: AgentState, decision: RoutingDecision) -> list[RoutingDecision]:
    return [*state.get("routing", []), decision]


class RemediationAgent:
    """Compiles and runs the remediation graph for a single failure pattern."""

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        self.graph = self._build()

    def _build(self):
        g: StateGraph = StateGraph(AgentState)
        g.add_node("investigate", self._investigate)
        g.add_node("reason_root_cause", self._reason_root_cause)
        g.add_node("generate_fix", self._generate_fix)
        g.add_node("draft_pr", self._draft_pr)
        g.add_node("open_pr", self._open_pr)
        g.add_node("notify", self._notify)

        g.add_edge(START, "investigate")
        g.add_edge("investigate", "reason_root_cause")
        g.add_conditional_edges(
            "reason_root_cause",
            self._confidence_gate,
            {"act": "generate_fix", "skip": "notify"},
        )
        g.add_edge("generate_fix", "draft_pr")
        g.add_edge("draft_pr", "open_pr")
        g.add_edge("open_pr", "notify")
        g.add_edge("notify", END)
        return g.compile()

    # --- Nodes ---

    def _investigate(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        events = state.get("sample_events", [])
        suspected = extract_stack_files(events)
        if not suspected and pattern.exception_type:
            suspected = search_codebase(self.ctx, pattern.exception_type)
        code_context = retrieve_code(self.ctx, suspected)
        return {"suspected_files": suspected, "code_context": code_context}

    def _reason_root_cause(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        suspected = state.get("suspected_files", [])
        prompt = (
            f"A recurring failure has been detected ({pattern.count} occurrences).\n"
            f"Title: {pattern.title}\n"
            f"Message: {pattern.representative_message}\n"
            f"Suspected files: {', '.join(suspected) or 'unknown'}\n\n"
            f"Code context:\n{state.get('code_context') or '(none)'}\n\n"
            "Explain the most likely root cause in 2-3 sentences."
        )
        response, decision = self.ctx.router.run(
            TaskType.ROOT_CAUSE, prompt, sensitivity=pattern.sensitivity
        )
        confidence = 0.8 if suspected else 0.3
        root_cause = RootCause(
            pattern_id=pattern.id,
            summary=response.text,
            suspected_files=suspected,
            confidence=confidence,
            reasoning=response.text,
        )
        return {"root_cause": root_cause, "routing": _append_routing(state, decision)}

    def _confidence_gate(self, state: AgentState) -> str:
        root_cause = state.get("root_cause")
        if root_cause and root_cause.confidence >= self.ctx.min_confidence:
            return "act"
        return "skip"

    def _generate_fix(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        fix, decision = generate_fix(
            self.ctx, pattern, state["root_cause"], state.get("code_context", "")
        )
        return {"fix": fix, "routing": _append_routing(state, decision)}

    def _draft_pr(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        root_cause = state["root_cause"]
        fix = state["fix"]
        prompt = (
            f"Write a clear, professional pull request description for this fix.\n"
            f"Failure: {pattern.title}\nRoot cause: {root_cause.summary}\n"
            f"Fix summary: {fix.summary}\n"
        )
        response, decision = self.ctx.router.run(
            TaskType.PR_DESCRIPTION, prompt, sensitivity=pattern.sensitivity
        )
        body = (
            f"## Summary\n{response.text}\n\n"
            f"## Root cause\n{root_cause.summary}\n\n"
            f"## Test plan\n{fix.test_plan}\n\n"
            f"---\n_Opened automatically by tvastr from {pattern.count} recurring "
            f"failures (fingerprint `{pattern.fingerprint}`)._"
        )
        draft = PullRequestDraft(
            pattern_id=pattern.id,
            title=f"fix: {pattern.title}",
            body=body,
            branch=f"tvastr/fix-{pattern.fingerprint}",
            changes=fix.changes,
        )
        return {"pr_draft": draft, "routing": _append_routing(state, decision)}

    def _open_pr(self, state: AgentState) -> AgentState:
        result = open_pull_request(self.ctx, state["pr_draft"])
        outcome = "pr_opened" if result.created else "failed"
        return {"pr_result": result, "outcome": outcome}

    def _notify(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        outcome = state.get("outcome", "skipped")
        if outcome == "pr_opened" and (result := state.get("pr_result")):
            message = f":wrench: tvastr opened a PR for *{pattern.title}* → {result.url}"
            notes = f"PR opened: {result.url}"
        else:
            message = (
                f":mag: tvastr flagged *{pattern.title}* ({pattern.count}x) for human "
                "review — root-cause confidence too low to auto-fix."
            )
            notes = "Escalated to human (low confidence)."
        send_notification(self.ctx, message)
        return {"outcome": outcome, "notes": notes}

    # --- Entry point ---

    def run(self, state: AgentState) -> AgentState:
        log.info("agent.run", pattern=state["pattern"].fingerprint)
        return self.graph.invoke(state)
