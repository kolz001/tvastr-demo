"""The remediation agent as a LangGraph state machine.

Flow::

    START -> investigate (agentic loop: search/read/list -> root_cause)
        -> (confidence gate)
        high -> ground_root_cause -> generate_fix -> compare_to_pr
             -> draft_pr -> open_pr -> notify -> END
        low  -> notify (skipped) -> END

    compare_to_pr benchmarks the agent's fix against the upstream PR (the
    ground-truth oracle) when one was discovered; it emits benchmark.compared
    or benchmark.skipped and never blocks the act path.

    ground_root_cause (live + TVASTR_DOC_GROUNDING only) validates the diagnosis
    against external docs via web_search before the fix; it never blocks the run.

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
    list_dir,
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

_DOC_GROUNDING_SYSTEM = (
    "You validate a bug diagnosis against authoritative external documentation."
)


def _append_routing(state: AgentState, decision: RoutingDecision) -> list[RoutingDecision]:
    return [*state.get("routing", []), decision]


_MAX_CONTEXT_FILES = 12  # cap on accumulated code_files
_MAX_INVESTIGATE_ROUNDS = 4

_INVESTIGATE_SYSTEM = (
    "You are a senior engineer debugging a reported bug by reading the codebase. "
    "Follow this method strictly:\n"
    "1. ROOT CAUSE FIRST: do not conclude until you have READ the actual code that "
    "proves the cause; if you have not, keep investigating or report low confidence.\n"
    "2. VERIFY THE REPORTER'S HYPOTHESIS: identify the real symptom AND any cause the "
    "reporter guessed, and treat the guess as a hypothesis to confirm against the code, "
    "not as fact.\n"
    "3. CROSS-REFERENCE: compare related/sibling code paths (read vs write vs delete) and "
    "look for the inconsistency that explains the bug.\n"
    "4. CITE EVIDENCE: your root_cause must reference specific file:line; set confidence by "
    "how well-corroborated it is; do not guess.\n\n"
    "Respond with ONLY JSON. To investigate further:\n"
    '{"thought": "...", "actions": [{"search": "terms"}, {"read_file": "path"}, '
    '{"list_dir": "dir"}]}\n'
    "When you have a proven root cause:\n"
    '{"root_cause": "2-4 sentences citing file:line", "suspected_files": ["path"], '
    '"confidence": 0.0-1.0, "done": true}'
)


def _clamp_confidence(value: object) -> float:
    try:
        return max(0.0, min(1.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def _parse_investigation(text: str) -> dict:
    """The investigator's JSON turn, or {} if unparseable."""
    parsed = extract_json(text)
    return parsed if isinstance(parsed, dict) else {}


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
        g.add_node("ground_root_cause", self._ground_root_cause)
        g.add_node("generate_fix", self._generate_fix)
        g.add_node("compare_to_pr", self._compare_to_pr)
        g.add_node("draft_pr", self._draft_pr)
        g.add_node("open_pr", self._open_pr)
        g.add_node("notify", self._notify)

        g.add_edge(START, "investigate")
        g.add_conditional_edges(
            "investigate",
            self._confidence_gate,
            {"act": "ground_root_cause", "skip": "notify"},
        )
        g.add_edge("ground_root_cause", "generate_fix")
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
        issue_body = state.get("issue_body") or pattern.representative_message or pattern.title
        self._emit("agent.node.start", "investigate", pattern=pattern.fingerprint)

        code_files: dict[str, str] = {}
        seen: set[str] = set()
        transcript: list[str] = []
        decisions: list[RoutingDecision] = []

        # Free seed: stack-trace files when a traceback is present.
        suspected = extract_stack_files(events)
        if suspected:
            self._emit("tool.call", "extract_stack_files", source="stack_trace", paths=suspected)
            fetched = retrieve_code_files(self.ctx, suspected)
            code_files.update(fetched)
            seen.update(suspected)

        root_cause: RootCause | None = None
        for round_i in range(_MAX_INVESTIGATE_ROUNDS):
            prompt = (
                f"Issue: {pattern.title}\n\n{issue_body[:4000]}\n\n"
                f"Code read so far:\n{format_code_for_prompt(code_files) or '(none)'}\n\n"
                f"Tool results so far:\n{chr(10).join(transcript) or '(none)'}\n\n"
                "Investigate further or finish with a proven root_cause."
            )
            try:
                response, decision = self.ctx.router.run(
                    TaskType.ROOT_CAUSE, prompt,
                    sensitivity=pattern.sensitivity, system=_INVESTIGATE_SYSTEM,
                )
            except Exception as exc:
                log.warning("agent.investigate.llm_failed", error=str(exc))
                break
            decisions.append(decision)
            parsed = _parse_investigation(response.text)

            if parsed.get("done") and parsed.get("root_cause"):
                root_cause = RootCause(
                    pattern_id=pattern.id,
                    summary=str(parsed["root_cause"]),
                    suspected_files=[str(p) for p in (parsed.get("suspected_files") or [])]
                    or list(code_files),
                    confidence=_clamp_confidence(parsed.get("confidence", 0.5)),
                    reasoning=response.text,
                )
                break

            actions = parsed.get("actions") or []
            if not actions:
                break  # unparseable / nothing proposed → stop
            self._emit("tool.call", "investigate.round", round=round_i + 1,
                       thought=str(parsed.get("thought", "")))
            for act in actions:
                if not isinstance(act, dict):
                    continue
                if "search" in act:
                    q = str(act["search"])
                    hits = search_codebase(self.ctx, q)
                    self._emit("tool.call", "search_codebase", query=q, paths=hits)
                    transcript.append(f"search {q!r} -> {hits}")
                elif "read_file" in act:
                    p = str(act["read_file"])
                    if p in seen or len(code_files) >= _MAX_CONTEXT_FILES:
                        continue
                    fetched = retrieve_code_files(self.ctx, [p], max_files=1)
                    code_files.update(fetched)
                    seen.add(p)
                    self._emit("tool.call", "retrieve_code_files", requested=1,
                               retrieved=len(fetched), paths=list(fetched.keys()))
                elif "list_dir" in act:
                    d = str(act["list_dir"])
                    entries = list_dir(self.ctx, d)
                    self._emit("tool.call", "list_dir", path=d, entries=entries)
                    transcript.append(f"list_dir {d!r} -> {entries}")

        if root_cause is None:
            root_cause = RootCause(
                pattern_id=pattern.id,
                summary="insufficient evidence to determine the root cause",
                suspected_files=list(code_files),
                confidence=0.0,
                reasoning="investigator did not converge within the round budget",
            )
        self._emit("agent.node.end", "investigate",
                   confidence=root_cause.confidence, summary=root_cause.summary,
                   files_read=len(code_files))
        routing = state.get("routing", [])
        return {
            "root_cause": root_cause,
            "suspected_files": root_cause.suspected_files,
            "code_files": code_files,
            "code_context": format_code_for_prompt(code_files),
            "routing": [*routing, *decisions],
        }

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

    def _ground_root_cause(self, state: AgentState) -> AgentState:
        pattern = state["pattern"]
        root_cause = state.get("root_cause")
        if not self.ctx.doc_grounding or root_cause is None:
            self._emit("doc.skipped", "ground_root_cause", reason="grounding disabled")
            return {}
        self._emit("agent.node.start", "ground_root_cause", pattern=pattern.fingerprint)
        prompt = (
            f"Failure: {pattern.title}\n"
            f"Current diagnosis: {root_cause.summary}\n\n"
            f"Code context:\n{state.get('code_context') or '(none)'}\n\n"
            "Validate this diagnosis against authoritative external documentation. "
            "Use web_search ONLY if the root cause depends on third-party API/library "
            "behavior (e.g. a renamed field or changed return shape in a dependency). "
            "Return ONLY the corrected root-cause summary in 2-4 sentences; if the "
            "original was correct, restate it concisely."
        )
        try:
            response, decision = self.ctx.router.run(
                TaskType.DOC_GROUNDING,
                prompt,
                sensitivity=pattern.sensitivity,
                system=_DOC_GROUNDING_SYSTEM,
                web_search=True,
            )
        except Exception as exc:  # never crash the run
            log.warning("agent.ground_root_cause.failed", error=str(exc))
            self._emit("doc.skipped", "ground_root_cause", reason=f"grounding error: {exc}")
            return {}
        grounded_summary = response.text.strip() or root_cause.summary
        changed = grounded_summary != root_cause.summary
        update: dict = {"summary": grounded_summary}
        if changed:
            update["reasoning"] = response.text
        new_root_cause = root_cause.model_copy(update=update)
        self._emit(
            "doc.grounded",
            "ground_root_cause",
            searched=bool(response.sources),
            changed=changed,
            sources=response.sources,
        )
        return {
            "root_cause": new_root_cause,
            "doc_sources": response.sources,
            "routing": _append_routing(state, decision),
        }

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
