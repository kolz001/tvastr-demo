"""Sandbox protocol contract — exercised against SubprocessSandbox so we
don't require Docker to run the suite."""

from __future__ import annotations

import shutil
import sys

from tvastr.config import Settings
from tvastr.domain import FileChange
from tvastr.verification import SubprocessSandbox, build_sandbox
from tvastr.verification.sandbox import DockerSandbox


def test_subprocess_sandbox_writes_files_and_runs_commands() -> None:
    sb = SubprocessSandbox()
    handle = sb.prepare()
    try:
        handle.write_file("hello.py", "print('hi from sandbox')\n")
        result = handle.run([sys.executable, "hello.py"], timeout_s=10)
        assert result.exit_code == 0
        assert "hi from sandbox" in result.stdout
        assert result.succeeded
    finally:
        handle.discard()
    assert not handle.root.exists()


def test_subprocess_sandbox_applies_file_changes() -> None:
    sb = SubprocessSandbox()
    handle = sb.prepare()
    try:
        handle.apply_changes(
            [
                FileChange(path="a.py", patched_content="print('a')\n", rationale="r"),
                FileChange(path="pkg/b.py", patched_content="print('b')\n", rationale="r"),
            ]
        )
        assert (handle.root / "a.py").read_text() == "print('a')\n"
        assert (handle.root / "pkg" / "b.py").read_text() == "print('b')\n"
    finally:
        handle.discard()


def test_subprocess_sandbox_reports_nonzero_exit() -> None:
    sb = SubprocessSandbox()
    handle = sb.prepare()
    try:
        handle.write_file(
            "boom.py", "raise ValueError('Missing required input variable')\n"
        )
        result = handle.run([sys.executable, "boom.py"], timeout_s=10)
        assert result.exit_code != 0
        assert not result.succeeded
        assert "ValueError" in result.stderr
    finally:
        handle.discard()


def test_subprocess_sandbox_honors_timeout() -> None:
    sb = SubprocessSandbox()
    handle = sb.prepare()
    try:
        handle.write_file("loop.py", "import time; time.sleep(10)\n")
        result = handle.run([sys.executable, "loop.py"], timeout_s=1)
        assert result.timed_out
        assert result.exit_code == -1
    finally:
        handle.discard()


def test_build_sandbox_forced_subprocess() -> None:
    settings = Settings(verify_sandbox="subprocess")
    assert isinstance(build_sandbox(settings), SubprocessSandbox)


def test_build_sandbox_forced_docker() -> None:
    settings = Settings(verify_sandbox="docker")
    assert isinstance(build_sandbox(settings), DockerSandbox)


def test_build_sandbox_auto_picks_docker_if_present() -> None:
    settings = Settings(verify_sandbox="auto")
    sandbox = build_sandbox(settings)
    if shutil.which("docker"):
        assert isinstance(sandbox, DockerSandbox)
    else:
        assert isinstance(sandbox, SubprocessSandbox)
