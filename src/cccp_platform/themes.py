"""Theme/taxonomy tagging: a keyword match against the controlled taxonomy,
NOT a trained classifier (production would use one, docs/architecture.md
§9.9) -- treat it as a placeholder, not a validated component.

Deliberately dependency-free (stdlib `re` only) and separate from
`azure_backends/sentiment_language.py`, even though that module re-exports
it for backward compatibility: this keyword match needs no Azure call, so
it must not live somewhere that imports the Azure SDK at module level --
that would force local/offline mode (apps/api/server.py, explicitly
stdlib-only by design, see pyproject.toml) to have azure-ai-textanalytics
installed just to tag a theme from plain text.
"""
from __future__ import annotations

import re

TAXONOMY = ("fees", "fraud_security", "card_issue", "digital_app", "cancellation", "retention",
            "savings", "product_enquiry", "service_quality", "resolution")

# Whole-word patterns (a prefix match where noted). Plain substring matching
# tagged "feel" as fees and "happy"/"apply"/"appreciate" as digital_app.
# Broadened beyond the most literal keywords (e.g. "offer"/"product") to
# also catch common natural phrasings that mean the same thing without
# using that exact word -- this taxonomy now doubles as the live trigger
# engine's (trigger.py) only text-derived signal, so a pattern too narrow
# to fire on ordinary dialogue silently under-triggers, not just under-tags
# for display.
_THEME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple((t, re.compile(p, re.I)) for t, p in (
    ("fees", r"\bfees?\b|\bcharges?\b"),
    ("fraud_security", r"\bfraud\w*|\bsecurity\b|\bscams?\b"),
    ("card_issue", r"\bcards?\b"),
    ("digital_app", r"\bapps?\b|\bdigital\b|\bonline banking\b"),
    ("cancellation", r"\bcancel\w*"),
    ("resolution", r"\bcomplaints?\b|\bresolved?\b|\bresolution\b"),
    ("savings", r"\bsav(?:e|es|ing|ings)\b|\bmoney aside\b|\bputting.{0,20}aside\b"),
    ("product_enquiry", r"\boffer\w*|\bproducts?\b|\bavailable\b|\boptions?\b|\binterested\b|"
                        r"\banything (?:new|else)\b|\bwhat.{0,20}\b(?:have|got|available)\b|\bwondered?\b"),
))


def tag_themes(text: str) -> list[str]:
    """Whole-word keyword match against the controlled taxonomy -- NOT a trained classifier."""
    return [theme for theme, pat in _THEME_PATTERNS if pat.search(text)]
