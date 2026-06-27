"""Tolerant JSON extraction shared by the LLM-backed analysis modules.

LLMs wrap JSON in prose ("Here's the analysis: {...}"), fence it, or precede it
with reasoning. A greedy ``re.search(r"\\{.*\\}")`` over-matches across nested
braces; instead we scan from each ``{`` and let ``raw_decode`` find the first
balanced object.
"""

from __future__ import annotations

import json


def extract_json(text: str) -> dict | None:
    """Return the first balanced JSON object in ``text``, or None.

    Scans from each ``{`` and uses ``raw_decode`` so trailing prose after the
    object is tolerated. Returns None if no ``{...}`` decodes to a dict.
    """
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = decoder.raw_decode(text[idx:])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
    return None


def extract_all_json(text: str) -> list[dict]:
    """Return every balanced top-level JSON object in ``text``, in order.

    LLMs sometimes emit several objects in one response (e.g. a thought object
    followed by a separate finish object). ``extract_json`` returns only the
    first; this collects them all so callers can merge.
    """
    decoder = json.JSONDecoder()
    out: list[dict] = []
    idx = text.find("{")
    while idx != -1:
        try:
            obj, end = decoder.raw_decode(text[idx:])
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
            continue
        if isinstance(obj, dict):
            out.append(obj)
        idx = text.find("{", idx + end)  # advance past the object just decoded
    return out
