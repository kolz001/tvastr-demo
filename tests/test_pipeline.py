from tvastr.analysis.pr_discovery import PrDiff, PrFile, PullRequestRef
from tvastr.pipeline import build_pipeline


def test_pipeline_runs_end_to_end_on_sample_logs(settings):
    run = build_pipeline(settings).run()

    assert run.events_ingested > 0
    assert run.patterns_detected > 0
    assert run.patterns_selected > 0

    # At least one recurring pattern should result in an opened (mock) PR.
    opened = [o for o in run.outcomes if o.outcome == "pr_opened"]
    assert opened, "expected at least one PR to be opened"
    assert opened[0].pull_request_url and opened[0].pull_request_url.startswith("https://")


def test_pipeline_records_hybrid_routing(settings):
    run = build_pipeline(settings).run()
    targets = {d["target"] for o in run.outcomes for d in o.routing}
    # A remediated pattern exercises cloud reasoning steps.
    assert "cloud" in targets


def test_pipeline_seeds_agent_state_with_pr(settings, recurring_events, monkeypatch):

    pipeline = build_pipeline(settings)
    seen = {}
    orig = pipeline.agent.run

    def _spy(state):
        seen.update(state)
        return orig(state)

    monkeypatch.setattr(pipeline.agent, "run", _spy)
    ref = PullRequestRef(30, "fix", "open", False, "u", 1)
    diff = PrDiff(files=[PrFile("x.py", "modified", 1, 0, "@@\n+x")])
    pipeline.run(events=recurring_events, pr_ref=ref, pr_diff=diff)
    assert seen.get("pr_ref") == ref
    assert seen.get("pr_diff") == diff
