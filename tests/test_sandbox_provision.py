from __future__ import annotations

import subprocess

from tvastr.verification.models import ProvisionResult
from tvastr.verification.sandbox import (
    SubprocessSandbox,
    distribution_for_path,
)


def test_distribution_for_path_integration():
    p = (
        "llama-index-integrations/vector_stores/"
        "llama-index-vector-stores-s3/llama_index/vector_stores/s3/base.py"
    )
    assert distribution_for_path(p) == "llama-index-vector-stores-s3"


def test_distribution_for_path_core():
    assert (
        distribution_for_path("llama-index-core/llama_index/core/base.py")
        == "llama-index-core"
    )


def test_distribution_for_path_flat_checkout_returns_none():
    # No integration dir above the import root → nothing to provision.
    assert distribution_for_path("llama_index/core/base.py") is None


def test_distribution_for_path_non_package_returns_none():
    assert distribution_for_path("examples/demo.ipynb") is None
    assert distribution_for_path("README.md") is None


def test_provision_result_empty_is_ok():
    r = ProvisionResult(requested=[], installed=[], failed=[], ok=True)
    assert r.ok and r.installed == []


def test_subprocess_provision_installs_and_injects_pythonpath(monkeypatch):
    sandbox = SubprocessSandbox()
    handle = sandbox.prepare()
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        # Simulate a successful `pip install --target` creating the deps dir.
        (handle.root / ".tvastr_deps").mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = handle.provision(["llama-index-vector-stores-s3"])
    assert result.ok
    assert result.installed == ["llama-index-vector-stores-s3"]
    assert "install" in captured["cmd"] and "--target" in captured["cmd"]

    # After provisioning, a real run must expose the deps dir on PYTHONPATH.
    monkeypatch.undo()
    out = handle.run(
        ["python", "-c", "import os; print(os.environ.get('PYTHONPATH', ''))"]
    )
    assert str(handle.root / ".tvastr_deps") in out.stdout
    handle.discard()


def test_subprocess_provision_empty_is_noop(monkeypatch):
    sandbox = SubprocessSandbox()
    handle = sandbox.prepare()

    def boom(*a, **k):  # provision must not invoke pip for an empty dist list
        raise AssertionError("pip should not run for empty dists")

    monkeypatch.setattr(subprocess, "run", boom)
    result = handle.provision([])
    assert result.ok and result.requested == []
    handle.discard()


def test_subprocess_provision_failure_reports_not_ok(monkeypatch):
    sandbox = SubprocessSandbox()
    handle = sandbox.prepare()

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="No matching distribution")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = handle.provision(["llama-index-does-not-exist"])
    assert not result.ok
    assert result.failed == ["llama-index-does-not-exist"]
    handle.discard()
