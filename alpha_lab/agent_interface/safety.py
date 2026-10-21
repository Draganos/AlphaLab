"""Safety primitives for the Agent Research Interface (roadmap Phase 9).

Everything an agent receives is data, never instructions. Text that did not
originate in AlphaLab's own code (news headlines, filing excerpts, company
descriptions, provider-supplied names, model-written prose) is an injection
surface, so it is wrapped as ``{"text": ..., "untrusted": true}`` after control
and bidirectional-override characters are removed, and length-capped.
"""

import re
from typing import Any

INTERFACE_VERSION = "agent-interface-v1"

# A response is bounded: ~24 KB is roughly 6k tokens. A tool that could exceed
# it pages instead (see `get_domain`/`list_evidence`).
MAX_RESPONSE_BYTES = 24_000
MAX_TEXT_CHARS = 400
# Strings longer than this are treated as free text even under an unlisted key.
LONG_STRING_CHARS = 120
_MAX_DEPTH = 12

# Keys whose string values are free text from outside this codebase (or
# written by a model from such text).
TEXT_KEYS = frozenset({
    "company", "company_name", "title", "headline", "summary", "description",
    "business_description", "text", "body", "content", "excerpt", "snippet",
    "reasoning", "rationale", "thesis", "reason", "reasons", "exclusion_reasons",
    "review_reasons", "positives", "risks", "catalysts", "contradictions",
    "evidence_gaps", "strengths", "weaknesses", "publisher", "name", "url",
    # short provider-supplied values: catalogue metadata and analyst events
    "sector", "industry", "exchange", "country", "asset_type", "security_type",
    "firm", "analyst", "action", "price_target_action", "from_grade", "to_grade",
})

# C0/C1 controls, zero-width characters, and the bidirectional
# embedding/override/isolate characters used to hide or reorder text.
_UNSAFE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"
    "\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)
_WHITESPACE = re.compile(r"\s+")


def has_unsafe_characters(text: str) -> bool:
    """True if `text` holds control, zero-width or bidirectional-override characters."""
    return _UNSAFE.search(text) is not None


def clean_text(text: str) -> str:
    """Remove unsafe characters and flatten whitespace (so a value cannot fake
    line structure such as a new "system:" line)."""
    return _WHITESPACE.sub(" ", _UNSAFE.sub("", text)).strip()


def wrap_untrusted(text: str, *, max_chars: int = MAX_TEXT_CHARS) -> dict[str, Any]:
    cleaned = clean_text(text)
    truncated = len(cleaned) > max_chars
    if truncated:
        cleaned = cleaned[: max_chars - 1].rstrip() + "…"
    return {"text": cleaned, "untrusted": True, "truncated": truncated}


def sanitize_value(value: Any, *, key: str | None = None, _depth: int = 0) -> Any:
    """Recursively wrap free text and clean every string. Short strings under
    other keys (labels, enum values, ids) stay plain, with unsafe characters
    removed."""
    if _depth > _MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        return {k: sanitize_value(v, key=str(k), _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item, key=key, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        if key in TEXT_KEYS or len(value) > LONG_STRING_CHARS:
            return wrap_untrusted(value)
        return clean_text(value)
    return value


def clean_value(value: Any, *, _depth: int = 0) -> Any:
    """Clean every string but wrap none: for answers composed entirely of
    AlphaLab's own static text (the methodology), which must not be labelled
    untrusted."""
    if _depth > _MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        return {k: clean_value(v, _depth=_depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean_value(item, _depth=_depth + 1) for item in value]
    return clean_text(value) if isinstance(value, str) else value


def collect_provenance_ids(data: Any) -> list[str]:
    """Every provenance id cited anywhere in a payload, sorted and unique."""
    found: set[str] = set()

    def walk(node: Any, depth: int = 0) -> None:
        if depth > _MAX_DEPTH + 4:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "provenance_id" and isinstance(v, str) and v:
                    found.add(v)
                elif k == "provenance_ids" and isinstance(v, list):
                    found.update(item for item in v if isinstance(item, str) and item)
                else:
                    walk(v, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)

    walk(data)
    return sorted(found)
