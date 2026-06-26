from __future__ import annotations

from tvastr.integrations.github import DryRunCodeHost, MockGitHubClient


def test_mock_get_file_at_ref_returns_content():
    host = MockGitHubClient("run-llama/llama_index")
    out = host.get_file_at_ref("a/b/base.py", "abc123")
    assert out is not None
    assert "abc123" in out  # ref is reflected so callers can tell versions apart


def test_mock_buggy_parent_sha_is_deterministic():
    host = MockGitHubClient("run-llama/llama_index")
    sha = host.buggy_parent_sha(21447)
    assert sha and isinstance(sha, str)
    assert host.buggy_parent_sha(21447) == sha  # stable


def test_dryrun_delegates_ref_methods():
    inner = MockGitHubClient("run-llama/llama_index")
    host = DryRunCodeHost(inner, "run-llama/llama_index")
    assert host.get_file_at_ref("x/base.py", "deadbeef") == inner.get_file_at_ref(
        "x/base.py", "deadbeef"
    )
    assert host.buggy_parent_sha(10) == inner.buggy_parent_sha(10)
