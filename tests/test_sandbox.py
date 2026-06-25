import json
from pathlib import Path

from tvastr.domain import FileChange
from tvastr.verification.sandbox import _DockerHandle, installed_module_path


def test_module_path_core():
    assert (
        installed_module_path("llama-index-core/llama_index/core/memory/vector_memory.py")
        == "llama_index.core.memory.vector_memory"
    )


def test_module_path_nested_integration():
    # The real failing case: `llms` is a valid identifier appearing BEFORE the
    # import root `llama_index`, so we must start after the LAST non-identifier dir.
    p = ("llama-index-integrations/llms/llama-index-llms-google-genai/"
         "llama_index/llms/google_genai/utils.py")
    assert installed_module_path(p) == "llama_index.llms.google_genai.utils"


def test_module_path_notebook_is_none():
    assert installed_module_path("docs/examples/x.ipynb") is None


def test_module_path_non_py_is_none():
    assert installed_module_path("llama-index-integrations/llms/x/README.md") is None


def test_module_path_bare_script_is_none():
    assert installed_module_path("script.py") is None


def test_module_path_plain_package():
    assert installed_module_path("pkg/sub/mod.py") == "pkg.sub.mod"


def _docker_handle(tmp_path: Path) -> _DockerHandle:
    return _DockerHandle(tmp_path, "tvastr-verify:img")


def _change(path: str) -> FileChange:
    return FileChange(path=path, patched_content="# patched\n", rationale="r")


class _FakeProc:
    def __init__(self):
        self.returncode = 0
        self.stdout = ""
        self.stderr = ""


def _capture_docker(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeProc()

    monkeypatch.setattr("tvastr.verification.sandbox.subprocess.run", fake_run)
    return calls


def test_baseline_run_is_readonly_and_unwrapped(tmp_path, monkeypatch):
    calls = _capture_docker(monkeypatch)
    h = _docker_handle(tmp_path)
    h.run(["python", "repro.py"])  # before any apply_changes
    cmd = calls[0]
    assert "--read-only" in cmd
    assert "sh" not in cmd  # not wrapped
    assert cmd[-2:] == ["python", "repro.py"]


def test_apply_changes_stages_importable_file(tmp_path):
    h = _docker_handle(tmp_path)
    h.apply_changes([_change("llama-index-core/llama_index/core/memory/vector_memory.py")])
    staged = tmp_path / ".tvastr_patch" / "llama_index.core.memory.vector_memory.py"
    manifest = tmp_path / ".tvastr_patch" / "manifest.json"
    bootstrap = tmp_path / ".tvastr_apply.py"
    assert staged.read_text() == "# patched\n"
    assert bootstrap.exists()
    assert json.loads(manifest.read_text()) == [
        ["llama_index.core.memory.vector_memory", "llama_index.core.memory.vector_memory.py"]
    ]
    assert h._patch_pending is True


def test_apply_changes_nonimportable_writes_repo_path(tmp_path):
    h = _docker_handle(tmp_path)
    h.apply_changes([_change("docs/examples/x.ipynb")])
    assert (tmp_path / "docs/examples/x.ipynb").read_text() == "# patched\n"
    assert not (tmp_path / ".tvastr_patch").exists()
    assert h._patch_pending is False


def test_patched_rerun_is_wrapped_and_not_readonly(tmp_path, monkeypatch):
    calls = _capture_docker(monkeypatch)
    h = _docker_handle(tmp_path)
    h.apply_changes([_change("llama-index-core/llama_index/core/memory/vector_memory.py")])
    h.run(["python", "repro.py"])
    cmd = calls[0]
    assert "--read-only" not in cmd          # relaxed for the patched run
    assert "--network=none" in cmd           # other hardening kept
    assert "--cap-drop=ALL" in cmd
    assert "--rm" in cmd
    joined = " ".join(cmd)
    assert "/work/.tvastr_apply.py && python repro.py" in joined
    assert cmd[cmd.index(h.image) + 1] == "sh"  # entrypoint is sh -c after the image


def test_bootstrap_resolves_and_copies(tmp_path):
    h = _docker_handle(tmp_path)
    h.apply_changes([_change("llama-index-core/llama_index/core/memory/vector_memory.py")])
    src = (tmp_path / ".tvastr_apply.py").read_text()
    assert "find_spec" in src
    assert "shutil.copyfile" in src
    assert ".origin" in src
    assert "except Exception" in src
