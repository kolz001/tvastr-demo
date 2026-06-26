from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tvastr.verification.models import ProvisionResult
from tvastr.verification.sandbox import (
    DockerSandbox,
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


# ─── Offline Docker tests (no real docker, no network) ────────────────────


def test_docker_provision_success(monkeypatch):
    """provision() on returncode 0 → ok True, _deps_provisioned True.
    The captured docker command must NOT contain --network=none or --read-only
    but MUST contain --rm, --cap-drop=ALL, pip, install, --target.
    """
    handle = DockerSandbox(image="img").prepare()
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = handle.provision(["llama-index-vector-stores-s3"])

    assert result.ok is True
    assert handle._deps_provisioned is True
    docker_cmd = captured["cmd"]
    assert "--network=none" not in docker_cmd
    assert "--read-only" not in docker_cmd
    assert "--rm" in docker_cmd
    # prep container must write as the host user so discard() can rmtree it
    user_idx = docker_cmd.index("--user")
    assert docker_cmd[user_idx + 1] == f"{os.getuid()}:{os.getgid()}"
    assert "--cap-drop=ALL" in docker_cmd
    assert "pip" in docker_cmd
    assert "install" in docker_cmd
    assert "--target" in docker_cmd
    # No tmpfs cap on the prep run: a RAM-backed /tmp overflows on the dep tree
    # pip unpacks. The prep container isn't --read-only, so /tmp uses the disk
    # overlay. (The hardened baseline/rerun run keeps its own tmpfs.)
    assert not any(str(a).startswith("--tmpfs") for a in docker_cmd)
    handle.discard()


def test_docker_provision_exception_returns_not_ok(monkeypatch):
    """When subprocess.run raises, provision returns ProvisionResult(ok=False)
    and does not propagate the exception.
    """
    handle = DockerSandbox(image="img").prepare()

    def boom(cmd, **kwargs):
        raise OSError("docker not found")

    monkeypatch.setattr(subprocess, "run", boom)
    result = handle.provision(["llama-index-vector-stores-s3"])

    assert isinstance(result, ProvisionResult)
    assert result.ok is False
    handle.discard()


def test_docker_run_after_provision_prepends_pythonpath(monkeypatch):
    """After a successful provision, run() produces a docker sh -c command
    whose inner string contains the prepend form:
    'export PYTHONPATH=/work/.tvastr_deps:${PYTHONPATH:-}'.
    """
    handle = DockerSandbox(image="img").prepare()

    def fake_provision(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_provision)
    handle.provision(["llama-index-vector-stores-s3"])

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    handle.run(["python", "x.py"])

    docker_cmd = captured["cmd"]
    # The last element is the sh -c string; find it.
    sh_c_str = " ".join(docker_cmd)
    assert "export PYTHONPATH=/work/.tvastr_deps" in sh_c_str
    assert "sh" in docker_cmd
    assert "-c" in docker_cmd
    handle.discard()


def test_docker_run_no_provision_no_sh_wrapper(monkeypatch):
    """With no prior provision, run() docker command ends with the literal
    args (no sh -c wrapper) and still contains --network=none and --read-only.
    """
    handle = DockerSandbox(image="img").prepare()
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    handle.run(["python", "x.py"])

    docker_cmd = captured["cmd"]
    # Command must end with the literal args, not a sh -c wrapper.
    assert docker_cmd[-2:] == ["python", "x.py"]
    assert "--network=none" in docker_cmd
    assert "--read-only" in docker_cmd
    handle.discard()


# ─── Opt-in docker smoke test (real network, skipped by default) ──────────


@pytest.mark.skipif(
    not (shutil.which("docker") and os.environ.get("TVASTR_VERIFY_DOCKER_SMOKE")),
    reason="opt-in docker smoke (set TVASTR_VERIFY_DOCKER_SMOKE=1 with the verify image built)",
)
def test_docker_provision_namespace_merge_smoke():
    from tvastr.verification.sandbox import DockerSandbox

    handle = DockerSandbox(image="tvastr-verify:llamaindex").prepare()
    try:
        result = handle.provision(["llama-index-vector-stores-postgres"])
        assert result.ok, f"provision failed: {result}"
        # The provisioned integration must import ALONGSIDE the image's core —
        # this is the PEP 420 namespace-merge assumption the whole feature relies on.
        run = handle.run([
            "python", "-c",
            "import llama_index.core; "
            "import llama_index.vector_stores.postgres; "
            "print('MERGE_OK')",
        ], timeout_s=120)
        assert run.exit_code == 0, run.stderr
        assert "MERGE_OK" in run.stdout
    finally:
        handle.discard()
