"""Deterministic analytics: sentiment trend + key themes.

Neither function calls an LLM. Per-interaction sentiment scores and taxonomy
tags are produced upstream (a sentiment classifier and a post-call tagging
job); this module only aggregates what already exists, so results are
reproducible and unit-testable without a model in the loop.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from statistics import mean

from .domain import Interaction, LiveCallSignal, SentimentTrend, ThemeInsight

TREND_METHOD = "sentiment-trend-v1"
THEME_METHOD = "theme-aggregation-v1"

# Thresholds are configuration, versioned together with the method name.
MIN_POINTS = 3
SLOPE_THRESHOLD = 0.05      # per interaction
DELTA_THRESHOLD = 0.15      # recent mean vs earlier mean


def _in_window(i: Interaction, as_of: date, window_days: int) -> bool:
    age = (as_of - i.occurred_at).days
    return 0 <= age <= window_days


def _ols_slope(ys: list[float]) -> float:
    """Ordinary least-squares slope of ys against their index (0..n-1)."""
    n = len(ys)
    xm = (n - 1) / 2
    ym = mean(ys)
    num = sum((x - xm) * (y - ym) for x, y in enumerate(ys))
    den = sum((x - xm) ** 2 for x in range(n))
    return num / den if den else 0.0


def analyse_sentiment_trend(
    interactions: list[Interaction],
    as_of: date,
    live: LiveCallSignal | None = None,
    window_days: int = 365,
) -> SentimentTrend:
    pts = sorted((i for i in interactions if _in_window(i, as_of, window_days)), key=lambda i: i.occurred_at)
    scores = [i.sentiment_score for i in pts]
    live_s = live.current_sentiment if live else None
    n = len(scores)
    if n < MIN_POINTS:
        return SentimentTrend("insufficient_data", n, round(mean(scores), 3) if scores else None,
                               None, None, None, live_s, TREND_METHOD)

    k = min(3, n // 2)
    recent, earlier = mean(scores[-k:]), mean(scores[:-k])
    delta = recent - earlier
    slope = _ols_slope(scores)
    if slope >= SLOPE_THRESHOLD and delta >= DELTA_THRESHOLD:
        label = "improving"
    elif slope <= -SLOPE_THRESHOLD and delta <= -DELTA_THRESHOLD:
        label = "deteriorating"
    else:
        label = "stable"
    return SentimentTrend(label, n, round(mean(scores), 3), round(recent, 3), round(earlier, 3),
                           round(slope, 3), live_s, TREND_METHOD)


def identify_key_themes(
    interactions: list[Interaction],
    as_of: date,
    live: LiveCallSignal | None = None,
    window_days: int = 365,
    recent_days: int = 90,
    half_life_days: float = 90.0,
    top_n: int = 5,
) -> tuple[ThemeInsight, ...]:
    """Recency-weighted theme prevalence for one customer.

    `recurring` means the theme appears in at least two interactions.
    `emerging` means it appears in the recent window (or the live call) but
    not in the earlier part of the look-back window. Open-ended theme
    *discovery* across the whole customer base is a separate, corpus-level
    batch job; this function only ranks a single customer's known themes.
    """
    weight: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    recent: set[str] = set()
    prior: set[str] = set()

    for i in interactions:
        if not _in_window(i, as_of, window_days):
            continue
        age = (as_of - i.occurred_at).days
        w = 0.5 ** (age / half_life_days)
        for t in set(i.themes):
            weight[t] += w
            count[t] += 1
            (recent if age <= recent_days else prior).add(t)

    if live:
        for t in set(live.active_themes):
            weight[t] += 1.0
            count[t] += 1
            recent.add(t)

    total = sum(weight.values())
    if not total:
        return ()
    ranked = sorted(weight, key=lambda t: (-weight[t], t))[:top_n]
    return tuple(
        ThemeInsight(t, count[t], round(weight[t] / total, 3), count[t] >= 2, t in recent and t not in prior)
        for t in ranked
    )
