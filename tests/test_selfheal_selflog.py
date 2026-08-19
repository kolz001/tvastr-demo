"""Tests for the self-heal continuous log-capture layer (selflog.py).

SelfLogWriter is the foundation every later self-heal stage reads from — Task 2
mines these files to detect tvastr's own recurring failures. It must be inert
by default and never break application logging, so failure isolation (never
raises, always returns event_dict unchanged) is as much the point here as
correctness of the happy path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import structlog

from tvastr.logging import configure_logging
from tvastr.selfheal.selflog import SelfLogWriter, selflog_path


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def test_writes_one_parseable_json_line_per_call(tmp_path: Path) -> None:
    writer = SelfLogWriter(tmp_path)
    result = writer(None, "info", {"event": "hello", "n": 1})

    assert result == {"event": "hello", "n": 1}
    path = selflog_path(tmp_path, _today())
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"event": "hello", "n": 1}


def test_selflog_path_naming(tmp_path: Path) -> None:
    assert selflog_path(tmp_path, "2026-08-19") == tmp_path / "tvastr-2026-08-19.jsonl"


def test_concurrent_writes_produce_exactly_n_intact_lines(tmp_path: Path) -> None:
    writer = SelfLogWriter(tmp_path)
    n_threads = 8
    per_thread = 50

    def _write(i: int) -> None:
        for j in range(per_thread):
            writer(None, "info", {"event": f"thread-{i}", "seq": j})

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    path = selflog_path(tmp_path, _today())
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n_threads * per_thread
    for line in lines:
        parsed = json.loads(line)  # raises if a line got interleaved/corrupted
        assert "event" in parsed
        assert "seq" in parsed


def test_rollover_prunes_old_file_keeps_recent_file(tmp_path: Path) -> None:
    old_path = selflog_path(tmp_path, "2026-01-01")
    old_path.write_text('{"event": "old"}\n', encoding="utf-8")

    recent_day = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    recent_path = selflog_path(tmp_path, recent_day)
    recent_path.write_text('{"event": "recent"}\n', encoding="utf-8")

    writer = SelfLogWriter(tmp_path, retention_days=30)
    writer(None, "info", {"event": "today"})

    assert not old_path.exists()
    assert recent_path.exists()
    assert selflog_path(tmp_path, _today()).exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_unwritable_dir_never_raises(tmp_path: Path) -> None:
    target = tmp_path / "selflogs"
    target.mkdir()
    target.chmod(0o500)
    writer = SelfLogWriter(target)
    try:
        result = writer(None, "info", {"event": "x"})
        assert result == {"event": "x"}
        assert not selflog_path(target, _today()).exists()
    finally:
        target.chmod(0o700)  # restore write perms so tmp_path teardown can clean up


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission bits")
def test_unwritable_target_warns_once_per_process(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "selflogs"
    target.mkdir()
    target.chmod(0o500)
    writer = SelfLogWriter(target)
    try:
        with caplog.at_level(logging.WARNING, logger="tvastr.selfheal.selflog"):
            result1 = writer(None, "info", {"event": "first"})
            result2 = writer(None, "info", {"event": "second"})
            result3 = writer(None, "info", {"event": "third"})

        assert result1 == {"event": "first"}
        assert result2 == {"event": "second"}
        assert result3 == {"event": "third"}
        warning_records = [
            r for r in caplog.records
            if r.name == "tvastr.selfheal.selflog" and r.levelname == "WARNING"
        ]
        assert len(warning_records) == 1
        assert "selflog capture failed" in warning_records[0].message
    finally:
        target.chmod(0o700)


def test_configure_logging_without_selflog_dir_installs_no_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tvastr.logging as logging_mod

    monkeypatch.setattr(logging_mod, "_CONFIGURED", False)
    configure_logging(level="INFO", json_output=False, selflog_dir=None)

    processors = structlog.get_config()["processors"]
    assert not any(isinstance(p, SelfLogWriter) for p in processors)


def test_configure_logging_with_selflog_dir_installs_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tvastr.logging as logging_mod

    monkeypatch.setattr(logging_mod, "_CONFIGURED", False)
    configure_logging(level="INFO", json_output=False, selflog_dir=tmp_path, retention_days=7)

    processors = structlog.get_config()["processors"]
    writers = [p for p in processors if isinstance(p, SelfLogWriter)]
    assert len(writers) == 1
    assert writers[0].dir == tmp_path
    assert writers[0].retention_days == 7
