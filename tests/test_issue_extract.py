from tvastr.agent.retrieval.issue_extract import extract_issue_files


def test_extracts_integration_path():
    body = (
        'Traceback:\n  File "/x/site-packages/llama_index/multi_modal_llms/ollama/'
        'base.py", line 28, in get_additional_kwargs\n    ...\n'
    )
    assert extract_issue_files(body) == [
        "llama-index-integrations/multi_modal_llms/"
        "llama-index-multi-modal-llms-ollama/llama_index/multi_modal_llms/ollama/base.py"
    ]


def test_extracts_core_path():
    body = 'File "/x/llama_index/core/program/mm.py", line 5, in run\n'
    assert extract_issue_files(body) == [
        "llama-index-core/llama_index/core/program/mm.py"
    ]


def test_drops_unmappable_and_dedupes():
    body = (
        'File "/app/user_script.py", line 1\n'
        'File "/x/llama_index/core/a.py", line 2\n'
        'File "/y/llama_index/core/a.py", line 9\n'  # dup
    )
    assert extract_issue_files(body) == ["llama-index-core/llama_index/core/a.py"]


def test_empty_body():
    assert extract_issue_files("") == []
