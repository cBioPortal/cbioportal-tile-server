"""Deterministic pathology query interpretation for embedding search."""

from __future__ import annotations

import re
from typing import Any


_NEGATION = re.compile(r"\b(?:without|excluding|exclude|except|but not|negative for|no)\b", re.I)
_SEPARATORS = re.compile(r"\s+(?:and|with|showing|containing)\s+", re.I)

# These expansions make the search useful when a user uses a portal term that
# does not occur verbatim in the QuiltNet training vocabulary.  The original
# phrase is always retained, so the model still receives the user's wording.
_EXPANSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("carcinoma", ("invasive carcinoma", "adenocarcinoma", "malignant epithelial tumor")),
    ("adenocarcinoma", ("gland-forming carcinoma", "malignant glands")),
    ("tumor", ("neoplasm", "malignancy")),
    ("lymphocyte", ("lymphocytic infiltrate", "tumor infiltrating lymphocytes")),
    ("necrosis", ("necrotic tissue", "tumor necrosis")),
    ("stroma", ("stromal tissue", "desmoplasia")),
    ("gland", ("glandular architecture", "malignant glands")),
    ("mucin", ("mucinous material", "mucin production")),
    ("normal", ("benign tissue", "non-neoplastic tissue")),
    ("adipose", ("fat tissue", "adipocytes")),
    ("inflammation", ("inflammatory infiltrate", "immune cells")),
)


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip(" ,.;:()[]{}\t\n")).strip()


def _unique(values: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean(value)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            result.append(cleaned)
            seen.add(key)
        if len(result) >= limit:
            break
    return result


def build_query_plan(query: str) -> dict[str, Any]:
    """Return a compact, editable plan without requiring a second LLM call."""
    normalized = _clean(query)
    if not normalized:
        return {"primary": "", "positive": [], "negative": [], "summary": ""}

    pieces = _NEGATION.split(normalized, maxsplit=1)
    positive_text = _clean(pieces[0])
    negative_text = _clean(pieces[1]) if len(pieces) == 2 else ""
    positive_parts = [part for part in _SEPARATORS.split(positive_text) if part]
    positive = _unique(positive_parts or [positive_text], 4)
    negative = _unique(
        [part for part in _SEPARATORS.split(negative_text) if part], 2
    )
    haystack = positive_text.casefold()
    for trigger, expansions in _EXPANSIONS:
        if trigger in haystack:
            positive.extend(expansions)
    for trigger, expansions in _EXPANSIONS:
        if trigger in negative_text.casefold():
            negative.extend(expansions[:1])
    positive = _unique(positive, 4)
    negative = _unique(negative, 2)
    summary = f"Searching for {positive[0] if positive else normalized}"
    if negative:
        summary += f"; excluding {', '.join(negative)}"
    return {
        "primary": positive[0] if positive else normalized,
        "positive": positive,
        "negative": negative,
        "summary": summary,
    }
