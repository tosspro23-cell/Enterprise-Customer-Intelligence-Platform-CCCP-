"""Real sentiment scoring via Azure AI Language, replacing the hand-labelled
`sentiment` field in the local demo scripts. Maps the service's
positive/neutral/negative confidence scores onto the same [-1, 1] scalar
`cccp_agent.domain.Interaction.sentiment_score` already uses, so the rest
of the pipeline (insights.py, policy.py) needs no changes to consume it.

Theme/taxonomy tagging has no equivalent managed API in this build --
production would use a small trained classifier over the controlled
taxonomy (docs/architecture.md §9.9). `tag_themes` here is a keyword match
against the same taxonomy words the synthetic fixtures already use. It is
explicitly NOT a trained classifier; treat it as a placeholder, not a
validated component.
"""
from __future__ import annotations

import functools
import os
import re

from azure.ai.textanalytics import TextAnalyticsClient
from azure.core.credentials import AzureKeyCredential

TAXONOMY = ("fees", "fraud_security", "card_issue", "digital_app", "cancellation", "retention",
            "savings", "product_enquiry", "service_quality", "resolution")


@functools.lru_cache(maxsize=1)
def _client() -> TextAnalyticsClient:
    # One client per process: a new client per call put connection setup
    # into every measured sentiment latency.
    return TextAnalyticsClient(os.environ["AZURE_LANGUAGE_ENDPOINT"],
                                AzureKeyCredential(os.environ["AZURE_LANGUAGE_KEY"]))


def analyse_sentiment(text: str) -> float:
    """Returns a [-1, 1] score: positive_confidence - negative_confidence."""
    client = _client()
    result = client.analyze_sentiment([text])[0]
    if result.is_error:
        raise RuntimeError(f"Language sentiment error: {result.error}")
    scores = result.confidence_scores
    return round(scores.positive - scores.negative, 3)


# Whole-word patterns (a prefix match where noted). Plain substring matching
# tagged "feel" as fees and "happy"/"apply"/"appreciate" as digital_app.
_THEME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple((t, re.compile(p, re.I)) for t, p in (
    ("fees", r"\bfees?\b|\bcharges?\b"),
    ("fraud_security", r"\bfraud\w*|\bsecurity\b|\bscams?\b"),
    ("card_issue", r"\bcards?\b"),
    ("digital_app", r"\bapps?\b|\bdigital\b|\bonline banking\b"),
    ("cancellation", r"\bcancel\w*"),
    ("resolution", r"\bcomplaints?\b|\bresolved?\b|\bresolution\b"),
    ("savings", r"\bsav(?:e|es|ing|ings)\b|\bmoney aside\b"),
    ("product_enquiry", r"\boffer\w*|\bproducts?\b"),
))


def tag_themes(text: str) -> list[str]:
    """Whole-word keyword match against the controlled taxonomy -- NOT a trained classifier."""
    return [theme for theme, pat in _THEME_PATTERNS if pat.search(text)]
