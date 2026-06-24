"""The remediation agent as a LangGraph state machine.

Flow::

    START -> investigate -> reason_root_cause -> (confidence gate)
        high → generate_fix → compare_to_pr → draft_pr → open_pr → notify → END
        low  → notify (skipped) → END

    reason_root_cause may loop through expand_context (bounded to 2 extra
    rounds) to fetch code its own analysis asked for before the gate fires.

    compare_to_pr benchmarks the agent's fix against the upstream PR (the
    ground-truth oracle) when one was discovered; it emits benchmark.compared
    or benchmark.skipped and never blocks the act path.

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
from tvastr.analysis._jsonutil import extract_json
from tvastr.analysis.fix_comparison import compare_fix_to_pr
from tvastr.domain import PullRequestDraft, RootCause, RoutingDecision
from tvastr.events import PipelineEvent
from tvastr.llm.router import TaskType
from tvastr.logging import get_logger

log = get_logger(__name__)


def _append_routing(state: AgentState, decision: RoutingDecision) -> list[RoutingDecision]:
    return [*state.get("routing", []), decision]


_EVIDENCE_CONFIDENCE = {"stack_trace": 0.8, "search": 0.6}
_MAX_EXPANSIONS = 2  # extra retrieval rounds beyond the first investigate pass
_MAX_CONTEXT_FILES = 12  # cap on accumulated code_files


def _search_query_from_message(message: str) -> str:
    """Search terms for a pattern with no exception type.

    Synthetic non-crashing events look like ``UnexpectedBehavior: <issue
    title>`` — the part after the colon is what's worth grepping for.
    """
    tail = message.split(":", 1)[-1].strip()
    return tail[:120]


def _parse_reasoning(text: str) -> tuple[str, bool, dict]:
    """Split a reasoning response into (summary, need_more_context, next_targets).

    The reasoning LLM is asked for JSON {"root_cause", "need_more_context",
    "next_targets": {"queries", "paths"}}. If no such object is present (e.g. a
    prose mock response), fall back to (text, False, empty) — a single pass,
    exactly the pre-loop behavior.
    """
    empty = {"queries": [], "paths": []}
    parsed = extract_json(text)
    if not parsed or not parsed.get("root_cause"):
        return text, False, empty
    summary = str(parsed["root_cause"])
    need_more = bool(parsed.get("need_more_context", False))
    targets = parsed.get("next_targets") or {}
    queries = [str(q) for q in (targets.get("queries") or []) if q]
    paths = [str(p) for p in (targets.get("paths") or []) if p]
    return summary, need_more, {"queries": queries, "paths": paths}


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
        g.add_node("expand_context", self._expand_context)
        g.add_node("generate_fix", self._generate_fix)
        g.add_node("compare_to_pr", self._compare_to_pr)
        g.add_node("draft_pr", self._draft_pr)
        g.add_node("open_pr", self._open_pr)
        g.add_node("notify", self._notify)

        g.add_edge(START, "investigate")
        g.add_edge("investigate", "reason_root_cause")
        g.add_conditional_edges(
            "reason_root_cause",
            self._after_reason,
            {"expand": "expand_context", "act": "generate_fix", "skip": "notify"},
        )
        g.add_edge("expand_context", "reason_root_cause")
        g.add_edge("generate_fix", "compare_to_pr")
        g.add_edge("compare_to_pr", "draft_pr")
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
            "retrieved_paths": set(suspected),
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
            "Diagnose the root cause. If the code context is insufficient to pinpoint "
            "the bug (the real fault may be in a file you have not seen yet), say so and "
            "propose where to look next.\n\n"
            'Respond ONLY with JSON: {"root_cause": "2-3 sentence explanation", '
            '"need_more_context": true|false, "next_targets": '
            '{"queries": ["code-search terms"], "paths": ["repo/file/paths"]}}. '
            "Set need_more_context to false and leave next_targets empty when the "
            "current context is enough to write the fix."
        )
        response, decision = self.ctx.router.run(
            TaskType.ROOT_CAUSE, prompt, sensitivity=pattern.sensitivity
        )
        summary, need_more, next_targets = _parse_reasoning(response.text)
        # Confidence is unchanged: it tracks how the FIRST evidence was found
        # (stack trace 0.8 / search 0.6 / none 0.3), not the loop.
        confidence = _EVIDENCE_CONFIDENCE.get(state.get("evidence_source", "none"), 0.3)
        root_cause = RootCause(
            pattern_id=pattern.id,
            summary=summary,
            suspected_files=suspected,
            confidence=confidence,
            reasoning=response.text,
        )
        self._emit(
            "agent.node.end",
            "reason_root_cause",
            confidence=confidence,
            summary=root_cause.summary,
            need_more_context=need_more,
        )
        return {
            "root_cause": root_cause,
            "need_more_context": need_more,
            "next_targets": next_targets,
            "routing": _append_routing(state, decision),
        }

    def _expand_context(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        iteration = state.get("retrieval_iterations", 0) + 1
        targets = state.get("next_targets") or {"queries": [], "paths": []}
        seen = set(state.get("retrieved_paths", set()))
        code_files = dict(state.get("code_files", {}))
        self._emit(
            "agent.node.start", "expand_context",
            pattern=pattern.fingerprint, iteration=iteration,
        )
        try:
            # New paths come from fresh searches + the LLM's explicit path picks.
            new_paths: list[str] = []
            for query in targets["queries"]:
                if query in seen:
                    continue
                seen.add(query)
                hits = search_codebase(self.ctx, query)
                self._emit("tool.call", "search_codebase", query=query, paths=hits)
                for hit in hits:
                    if hit not in seen and hit not in code_files and hit not in new_paths:
                        new_paths.append(hit)
            for path in targets["paths"]:
                if path not in seen and path not in code_files and path not in new_paths:
                    new_paths.append(path)

            budget = max(0, _MAX_CONTEXT_FILES - len(code_files))
            to_fetch = new_paths[:budget]
            fetched = retrieve_code_files(self.ctx, to_fetch, max_files=budget) if to_fetch else {}
            missing = [p for p in to_fetch if p not in fetched]
            self._emit(
                "tool.call", "retrieve_code_files",
                requested=len(to_fetch), retrieved=len(fetched), paths=list(fetched.keys()),
            )
            code_files.update(fetched)
            seen.update(to_fetch)
            self._emit(
                "agent.node.end", "expand_context",
                files_added=len(fetched), missing_paths=missing, total_files=len(code_files),
            )
            return {
                "code_files": code_files,
                "code_context": format_code_for_prompt(code_files),
                "retrieved_paths": seen,
                "retrieval_iterations": iteration,
            }
        except Exception as exc:  # never crash the run — bump the counter so the cap stops us
            log.warning("agent.expand_context.failed", error=str(exc))
            self._emit(
                "agent.node.end", "expand_context",
                files_added=0, missing_paths=[], error=str(exc),
            )
            return {"retrieval_iterations": iteration}

    def _should_expand(self, state: AgentState) -> bool:
        if not state.get("need_more_context"):
            return False
        if state.get("retrieval_iterations", 0) >= _MAX_EXPANSIONS:
            return False
        targets = state.get("next_targets") or {"queries": [], "paths": []}
        seen = state.get("retrieved_paths", set())
        fresh = [t for t in (targets["queries"] + targets["paths"]) if t not in seen]
        return bool(fresh)

    def _after_reason(self, state: AgentState) -> str:
        """Route out of reasoning: loop to expand_context, or run the gate."""
        if self._should_expand(state):
            return "expand"
        return self._confidence_gate(state)

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

    def _compare_to_pr(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        pr_ref = state.get("pr_ref")
        pr_diff = state.get("pr_diff")
        if pr_ref is None or pr_diff is None:
            self._emit("benchmark.skipped", "compare_to_pr", reason="no upstream PR found")
            return {}
        self._emit("agent.node.start", "compare_to_pr", pr=pr_ref.number)
        try:
            comparison, decision = compare_fix_to_pr(
                pattern.title,
                state["root_cause"].summary,
                state["fix"],
                pr_ref,
                pr_diff,
                self.ctx.router,
            )
        except Exception as exc:  # never crash the run
            log.warning("agent.compare_to_pr.failed", error=str(exc))
            self._emit("benchmark.skipped", "compare_to_pr", reason=f"comparison error: {exc}")
            return {}
        self._emit(
            "benchmark.compared",
            "compare_to_pr",
            verdict=comparison.verdict,
            same_root_cause=comparison.same_root_cause,
            equivalence=comparison.equivalence,
            files_both=comparison.files_both,
            files_ours_only=comparison.files_ours_only,
            files_theirs_only=comparison.files_theirs_only,
            rationale=comparison.rationale,
            confidence=comparison.confidence,
            pr_number=pr_ref.number,
            pr_url=pr_ref.url,
        )
        return {"fix_comparison": comparison, "routing": _append_routing(state, decision)}

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
