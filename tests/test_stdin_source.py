import io

import pytest

from tvastr.ingestion import StdinLogSource


def test_stdin_source_parses_jsonl_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = (
        '{"service": "svc", "message": "ValueError: bad input"}\n'
        "# this is a comment, skip me\n"
        "\n"  # blank line, skip
        '{"service": "svc", "message": "RuntimeError: boom"}\n'
        "this line is not json — should be skipped, not crash the stream\n"
        '{"service": "svc", "message": "KeyError: nope"}\n'
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))

    events = list(StdinLogSource().read())
    assert [e.message for e in events] == [
        "ValueError: bad input",
        "RuntimeError: boom",
        "KeyError: nope",
    ]


def test_stdin_source_handles_empty_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert list(StdinLogSource().read()) == []
