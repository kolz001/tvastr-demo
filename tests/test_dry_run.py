from tvastr.config import Settings
from tvastr.integrations.github import DryRunCodeHost, MockGitHubClient, build_code_host
from tvastr.pipeline import build_pipeline


def test_build_code_host_wraps_when_dry_run_set() -> None:
    settings = Settings(use_mocks=True, audit_backend="memory", dry_run=True)
    host = build_code_host(settings)
    assert isinstance(host, DryRunCodeHost)
    assert isinstance(host.inner, MockGitHubClient)


def test_build_code_host_unwrapped_when_dry_run_false() -> None:
    settings = Settings(use_mocks=True, audit_backend="memory", dry_run=False)
    host = build_code_host(settings)
    assert not isinstance(host, DryRunCodeHost)


def test_pipeline_in_dry_run_records_draft_but_opens_no_pr() -> None:
    settings = Settings(
        use_mocks=True, audit_backend="memory", dry_run=True, recurrence_threshold=3
    )
    pipeline = build_pipeline(settings)
    run = pipeline.run()

    assert run.patterns_selected > 0
    assert run.outcomes, "expected at least one outcome"

    dry = [o for o in run.outcomes if o.outcome == "dry_run"]
    assert dry, f"expected dry_run outcomes, got {[o.outcome for o in run.outcomes]}"

    for outcome in dry:
        # No real PR URL is surfaced in dry-run.
        assert outcome.pull_request_url is None
        # But the proposed draft is captured so a human can inspect it.
        assert outcome.pr_title and outcome.pr_title.startswith("fix:")
        assert outcome.pr_branch and outcome.pr_branch.startswith("tvastr/fix-")
        assert outcome.pr_changes, "expected at least one proposed file change"

    # And the underlying mock client never recorded a PR opening.
    assert isinstance(pipeline.agent.ctx.code_host, DryRunCodeHost)
    inner = pipeline.agent.ctx.code_host.inner
    assert isinstance(inner, MockGitHubClient)
    assert inner.opened_prs == []
    # The decorator captured the drafts for traceability.
    assert pipeline.agent.ctx.code_host.captured_drafts


def test_dry_run_url_uses_sentinel_scheme() -> None:
    settings = Settings(
        use_mocks=True, audit_backend="memory", dry_run=True, recurrence_threshold=3
    )
    host = build_code_host(settings)
    assert isinstance(host, DryRunCodeHost)

    from tvastr.domain import PullRequestDraft

    draft = PullRequestDraft(
        pattern_id="p1", title="fix: x", body="b", branch="tvastr/fix-abc"
    )
    result = host.open_pull_request(draft)
    assert result.dry_run is True
    assert result.created is False
    assert result.url.startswith("dry-run://")
