"""Dual-path sandbox config for docker-out-of-docker (offline; argv only)."""

from __future__ import annotations

from pathlib import Path

import pydantic
import pytest

from tvastr.config import Settings
from tvastr.verification.sandbox import DockerSandbox


def test_settings_reject_host_root_without_work_root():
    with pytest.raises(pydantic.ValidationError):
        Settings(use_mocks=True, sandbox_host_work_root="/host/sandbox")


def test_docker_handle_mounts_host_path(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr("tvastr.verification.sandbox.subprocess.run", fake_run)
    work = tmp_path / "work"
    work.mkdir()
    host = Path("/host/visible/sandbox")
    sb = DockerSandbox(image="img", work_root=work, host_work_root=host)
    handle = sb.prepare()
    # files land under the (container-side) work root
    assert str(handle.root).startswith(str(work))
    handle.run(["python", "-V"], timeout_s=5)
    argv = captured["argv"]
    mount = next(a for i, a in enumerate(argv) if argv[i - 1] == "-v")
    # the docker -v mount uses the HOST view with the same per-run dir name
    assert mount == f"{host / handle.root.name}:/work:rw"


def test_docker_handle_default_paths_unchanged(tmp_path, monkeypatch):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr("tvastr.verification.sandbox.subprocess.run", fake_run)
    sb = DockerSandbox(image="img")
    handle = sb.prepare()
    handle.run(["python", "-V"], timeout_s=5)
    mount = next(a for i, a in enumerate(captured["argv"])
                 if captured["argv"][i - 1] == "-v")
    assert mount == f"{handle.root}:/work:rw"  # byte-identical to today
    handle.discard()
