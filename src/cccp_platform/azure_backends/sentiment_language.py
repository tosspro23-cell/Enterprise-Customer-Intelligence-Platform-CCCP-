"""Real sentiment scoring via Azure AI Language, replacing the hand-labelled
`sentiment` field in the local demo scripts. Maps the service's
positive/neutral/negative confidence scores onto the same [-1, 1] scalar
`cccp_agent.domain.Interaction.sentiment_score` already uses, so the rest
of the pipeline (insights.py, policy.py) needs no changes to consume it.

Theme/taxonomy tagging has no equivalent managed API in this build and
needs no Azure call at all -- it now lives in ..themes (dependency-free,
so local/offline mode can use it too) and is re-exported here only so
existing `from .sentiment_language import analyse_sentiment, tag_themes`
imports keep working unchanged.
"""
from __future__ import annotations

import functools
import os

from azure.ai.textanalytics import TextAnalyticsClient
from azure.core.credentials import AzureKeyCredential

from ..themes import TAXONOMY, tag_themes  # noqa: F401 -- re-exported


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
