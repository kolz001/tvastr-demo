from tvastr.verification.sandbox import installed_module_path


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
