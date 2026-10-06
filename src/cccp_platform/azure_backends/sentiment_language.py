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

import os

from azure.ai.textanalytics import TextAnalyticsClient
from azure.core.credentials import AzureKeyCredential

TAXONOMY = ("fees", "fraud_security", "card_issue", "digital_app", "cancellation", "retention",
            "savings", "product_enquiry", "service_quality", "resolution")


def _client() -> TextAnalyticsClient:
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


def tag_themes(text: str) -> list[str]:
    """Keyword match against the controlled taxonomy -- NOT a trained classifier."""
    low = text.lower()
    hits = []
    if "fee" in low:
        hits.append("fees")
    if "fraud" in low or "security" in low or "scam" in low:
        hits.append("fraud_security")
    if "card" in low:
        hits.append("card_issue")
    if "app" in low or "digital" in low:
        hits.append("digital_app")
    if "cancel" in low:
        hits.append("cancellation")
    if "complaint" in low or "resolve" in low or "resolution" in low:
        hits.append("resolution")
    if "saving" in low or "save" in low or "money aside" in low:
        hits.append("savings")
    if "offer" in low or "product" in low:
        hits.append("product_enquiry")
    return hits
