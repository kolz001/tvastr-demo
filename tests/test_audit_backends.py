from pathlib import Path

from tvastr.config import Settings
from tvastr.domain import AuditRecord
from tvastr.storage import FileAuditStore, InMemoryAuditStore, build_audit_store


def _record(outcome: str = "dry_run") -> AuditRecord:
    return AuditRecord(
        pattern_id="p1",
        pattern_title="ValueError in svc",
        outcome=outcome,
        notes="…",
    )


def test_build_audit_store_picks_memory() -> None:
    store = build_audit_store(Settings(audit_backend="memory"))
    assert isinstance(store, InMemoryAuditStore)


def test_build_audit_store_picks_file(tmp_path: Path) -> None:
    store = build_audit_store(
        Settings(audit_backend="file", audit_file_path=str(tmp_path / "a.jsonl"))
    )
    assert isinstance(store, FileAuditStore)


def test_build_audit_store_rejects_unknown_backend() -> None:
    # Bypass pydantic by constructing then mutating — the runtime guard should still fire.
    s = Settings(audit_backend="memory")
    object.__setattr__(s, "audit_backend", "bogus")
    import pytest

    with pytest.raises(ValueError, match="Unknown audit backend"):
        build_audit_store(s)


def test_file_audit_store_appends_jsonl_and_reads_back(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    store = FileAuditStore(path)
    a, b = _record("dry_run"), _record("pr_opened")
    store.save(a)
    store.save(b)

    assert path.exists()
    text = path.read_text("utf-8")
    assert text.count("\n") == 2

    loaded = store.all()
    assert [r.outcome for r in loaded] == ["dry_run", "pr_opened"]
    assert loaded[0].pattern_id == a.pattern_id


def test_file_audit_store_handles_missing_file_for_all(tmp_path: Path) -> None:
    store = FileAuditStore(tmp_path / "nope.jsonl")
    assert store.all() == []


def test_file_audit_store_creates_parent_directory(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "c" / "audit.jsonl"
    store = FileAuditStore(nested)
    store.save(_record())
    assert nested.exists()


def test_file_audit_store_skips_malformed_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text("not json\n" + _record().model_dump_json() + "\n", encoding="utf-8")
    store = FileAuditStore(path)
    records = store.all()
    assert len(records) == 1
