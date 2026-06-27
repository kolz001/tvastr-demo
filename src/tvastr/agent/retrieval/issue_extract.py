"""Extract suspected source files from an issue's traceback.

Maps an installed/import path (``llama_index/<cat>/<name>/<rest>``) to its
monorepo repo path so the investigator can read it at the issue-era ref. Mapping
is best-effort: unmappable paths are dropped (issue-era ``list_dir`` is the
safety net).
"""

from __future__ import annotations

import re

_FILE_RE = re.compile(r'File "([^"]+)", line \d+')


def _to_repo_path(traceback_path: str) -> str | None:
    p = traceback_path.replace("\\", "/")
    idx = p.rfind("llama_index/")
    if idx == -1:
        return None
    rel = p[idx:]  # llama_index/<...>
    parts = rel.split("/")
    if len(parts) < 3:  # need llama_index/<cat>/<...>
        return None
    if parts[1] == "core":
        return f"llama-index-core/{rel}"
    cat, name = parts[1], parts[2]
    # Both the category AND the provider name are hyphenated in the dist dir
    # (e.g. llama_index/llms/azure_openai → llama-index-llms-azure-openai).
    dist = f"llama-index-{cat.replace('_', '-')}-{name.replace('_', '-')}"
    return f"llama-index-integrations/{cat}/{dist}/{rel}"


def extract_issue_files(issue_body: str) -> list[str]:
    if not issue_body:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _FILE_RE.finditer(issue_body):
        repo_path = _to_repo_path(m.group(1))
        if repo_path and repo_path not in seen:
            seen.add(repo_path)
            out.append(repo_path)
    return out
