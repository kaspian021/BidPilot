"""Tiny, dependency-free helpers to read a JSON object out of an LLM answer.

Free models often wrap JSON in fences, add a sentence before it, or return
``{"action": …}`` followed by chatter. Every caller needs the same forgiving
parser, so it lives here instead of being copied into each module.
"""

from __future__ import annotations

import json
import re
from typing import Any


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of an LLM answer (fences, chatter, …)."""
    text = str(raw or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


__all__ = ["parse_json_object"]
