"""Scoped regression discovery + pytest summary parsing."""

from __future__ import annotations

from pathlib import Path

from tvastr.domain import FileChange
from tvastr.verification.regression import _parse_pytest_summary, discover_scoped_tests


def test_discover_finds_test_by_filename_match(tmp_path: Path) -> None:
    (tmp_path / "src" / "myapp").mkdir(parents=True)
    (tmp_path / "src" / "myapp" / "core.py").write_text("# code\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_core.py").write_text("def test_x(): pass\n")
    (tmp_path / "tests" / "test_unrelated.py").write_text("def test_y(): pass\n")

    changes = [FileChange(path="src/myapp/core.py", patched_content="# patched\n")]
    scoped = discover_scoped_tests(changes, tmp_path)
    assert any("test_core.py" in s for s in scoped)
    assert not any("test_unrelated.py" in s for s in scoped)


def test_discover_finds_test_by_import_match(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "mod.py").write_text("VALUE = 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_features.py").write_text(
        "from src.pkg.mod import VALUE\ndef test_v(): assert VALUE == 1\n"
    )

    changes = [FileChange(path="src/pkg/mod.py", patched_content="VALUE = 2\n")]
    scoped = discover_scoped_tests(changes, tmp_path)
    assert any("test_features.py" in s for s in scoped)


def test_discover_skips_venv_and_site_packages(tmp_path: Path) -> None:
    venv = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages" / "somepkg"
    venv.mkdir(parents=True)
    (venv / "test_inside_venv.py").write_text("# don't pick me up\n")

    changes = [FileChange(path="anything.py", patched_content="")]
    scoped = discover_scoped_tests(changes, tmp_path)
    assert not scoped


def test_discover_returns_empty_for_nonexistent_root() -> None:
    scoped = discover_scoped_tests(
        [FileChange(path="x.py", patched_content="")], Path("/no/such/dir")
    )
    assert scoped == []


def test_pytest_summary_parser_counts_each_kind() -> None:
    output = "============ 7 passed, 2 failed, 1 skipped in 3.21s ============"
    counts = _parse_pytest_summary(output)
    assert counts["passed"] == 7
    assert counts["failed"] == 2
    assert counts["skipped"] == 1
    assert counts["errors"] == 0


def test_pytest_summary_handles_errors_plural() -> None:
    output = "= 3 errors, 1 passed ="
    counts = _parse_pytest_summary(output)
    assert counts["errors"] == 3
    assert counts["passed"] == 1
