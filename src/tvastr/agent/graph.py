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
    format_code_for_prompt,
    generate_fix,
    open_pull_request,
    retrieve_code_files,
    search_codebase,
    send_notification,
)
from tvastr.domain import PullRequestDraft, RootCause, RoutingDecision
from tvastr.events import PipelineEvent
from tvastr.llm.router import TaskType
from tvastr.logging import get_logger

log = get_logger(__name__)


def _append_routing(state: AgentState, decision: RoutingDecision) -> list[RoutingDecision]:
    return [*state.get("routing", []), decision]


_EVIDENCE_CONFIDENCE = {"stack_trace": 0.8, "search": 0.6}


def _search_query_from_message(message: str) -> str:
    """Search terms for a pattern with no exception type.

    Synthetic non-crashing events look like ``UnexpectedBehavior: <issue
    title>`` — the part after the colon is what's worth grepping for.
    """
    tail = message.split(":", 1)[-1].strip()
    return tail[:120]


class RemediationAgent:
    """Compiles and runs the remediation graph for a single failure pattern."""

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx
        self.graph = self._build()

    def _emit(self, type_: str, step: str, **payload: object) -> None:
        self.ctx.event_sink.emit(
            PipelineEvent(
                type=type_,  # type: ignore[arg-type]
                layer="agent",
                step=step,
                run_id=self.ctx.run_id,
                payload=dict(payload),
            )
        )

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
        self._emit("agent.node.start", "investigate", pattern=pattern.fingerprint)

        evidence_source = "none"
        suspected = extract_stack_files(events)
        if suspected:
            evidence_source = "stack_trace"
            self._emit(
                "tool.call",
                "extract_stack_files",
                source="stack_trace",
                paths=suspected,
            )
        else:
            # No stack trace: grep for the exception type, or — for
            # non-crashing reports (synthetic "UnexpectedBehavior: <title>"
            # events) — for the behavior description after the colon.
            query = pattern.exception_type or _search_query_from_message(
                pattern.representative_message
            )
            if query:
                suspected = search_codebase(self.ctx, query)
                if suspected:
                    evidence_source = "search"
                self._emit(
                    "tool.call",
                    "search_codebase",
                    query=query,
                    paths=suspected,
                )

        code_files = retrieve_code_files(self.ctx, suspected)
        self._emit(
            "tool.call",
            "retrieve_code_files",
            requested=len(suspected),
            retrieved=len(code_files),
            paths=list(code_files.keys()),
        )
        self._emit(
            "agent.node.end",
            "investigate",
            suspected_files=suspected,
            files_retrieved=len(code_files),
        )
        return {
            "suspected_files": suspected,
            "evidence_source": evidence_source,
            "code_files": code_files,
            "code_context": format_code_for_prompt(code_files),
        }

    def _reason_root_cause(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        suspected = state.get("suspected_files", [])
        self._emit("agent.node.start", "reason_root_cause", pattern=pattern.fingerprint)
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
        # Confidence tracks evidence strength: a stack trace pins the file
        # (0.8); a code-search hit is circumstantial (0.6 — still above the
        # default 0.5 gate, but honest about it); nothing found escalates (0.3).
        confidence = _EVIDENCE_CONFIDENCE.get(state.get("evidence_source", "none"), 0.3)
        root_cause = RootCause(
            pattern_id=pattern.id,
            summary=response.text,
            suspected_files=suspected,
            confidence=confidence,
            reasoning=response.text,
        )
        self._emit(
            "agent.node.end",
            "reason_root_cause",
            confidence=confidence,
            summary=root_cause.summary,
        )
        return {"root_cause": root_cause, "routing": _append_routing(state, decision)}

    def _confidence_gate(self, state: AgentState) -> str:
        root_cause = state.get("root_cause")
        confidence = root_cause.confidence if root_cause else 0.0
        decision = "act" if confidence >= self.ctx.min_confidence else "skip"
        self._emit(
            "agent.node.start",
            "confidence_gate",
            confidence=confidence,
            threshold=self.ctx.min_confidence,
            decision=decision,
        )
        return decision

    def _generate_fix(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        self._emit("agent.node.start", "generate_fix", pattern=pattern.fingerprint)
        fix, decision = generate_fix(
            self.ctx, pattern, state["root_cause"], state.get("code_files", {})
        )
        self._emit(
            "fix.generated",
            "generate_fix",
            files=[c.path for c in fix.changes],
            summary=fix.summary,
            test_plan=fix.test_plan,
            diffs={c.path: c.diff for c in fix.changes if c.diff},
            # Patched content + rationales travel in the event payload so the
            # verifier can reconstruct the full FixProposal from a persisted run
            # without re-running the agent.
            patched_files={c.path: c.patched_content for c in fix.changes},
            rationales={c.path: c.rationale for c in fix.changes},
        )
        return {"fix": fix, "routing": _append_routing(state, decision)}

    def _draft_pr(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        root_cause = state["root_cause"]
        fix = state["fix"]
        self._emit("agent.node.start", "draft_pr", pattern=pattern.fingerprint)
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
        self._emit(
            "pr.drafted",
            "draft_pr",
            title=draft.title,
            branch=draft.branch,
            files=[c.path for c in draft.changes],
            body=body,
        )
        return {"pr_draft": draft, "routing": _append_routing(state, decision)}

    def _open_pr(self, state: AgentState) -> AgentState:
        self._emit("agent.node.start", "open_pr", branch=state["pr_draft"].branch)
        result = open_pull_request(self.ctx, state["pr_draft"])
        if result.dry_run:
            outcome = "dry_run"
            self._emit("pr.dry_run", "open_pr", url=result.url, branch=result.branch)
        elif result.created:
            outcome = "pr_opened"
            self._emit("pr.opened", "open_pr", url=result.url, number=result.number)
        else:
            outcome = "failed"
            self._emit("pr.failed", "open_pr", reason="create_pull returned created=False")
        return {"pr_result": result, "outcome": outcome}

    def _notify(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        outcome = state.get("outcome", "skipped")
        self._emit("agent.node.start", "notify", outcome=outcome)
        if outcome == "pr_opened" and (result := state.get("pr_result")):
            message = f":wrench: tvastr opened a PR for *{pattern.title}* → {result.url}"
            notes = f"PR opened: {result.url}"
        elif outcome == "dry_run" and (draft := state.get("pr_draft")):
            files = ", ".join(c.path for c in draft.changes) or "(none)"
            message = (
                f":construction: tvastr [DRY-RUN] would open PR for *{pattern.title}* — "
                f"{len(draft.changes)} file(s): {files}"
            )
            notes = f"Dry-run: no PR created. Proposed files: {files}"
        else:
            message = (
                f":mag: tvastr flagged *{pattern.title}* ({pattern.count}x) for human "
                "review — root-cause confidence too low to auto-fix."
            )
            notes = "Escalated to human (low confidence)."
        send_notification(self.ctx, message)
        self._emit("notify.sent", "notify", channel="slack", message=message)
        return {"outcome": outcome, "notes": notes}

    # --- Entry point ---

    def run(self, state: AgentState) -> AgentState:
        log.info("agent.run", pattern=state["pattern"].fingerprint)
        return self.graph.invoke(state)
