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
