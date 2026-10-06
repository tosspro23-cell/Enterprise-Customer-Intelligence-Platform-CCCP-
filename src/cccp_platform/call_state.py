"""Hot call state. In production this lives in a low-latency store with a
per-call TTL (module M5 in docs/architecture.md); here it is one mutable
object per call, held in memory only for the life of the simulated call.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CallState:
    call_id: str
    customer_id: str
    agent_id: str
    status: str = "active"                       # active | ended
    current_sentiment: float | None = None        # rolling, EWMA
    sentiment_series: list[float] = field(default_factory=list)
    active_themes: list[str] = field(default_factory=list)  # most-recent-first, capped
    commercial_state: str | None = None            # last decision outcome seen on this call
    trace_id: str = ""


ROLLING_ALPHA = 0.5     # recent utterances dominate, but a single spike doesn't whiplash the state
THEME_CAP = 5


def update_sentiment(state: CallState, score: float) -> float:
    state.sentiment_series.append(score)
    state.current_sentiment = (score if state.current_sentiment is None
                                else ROLLING_ALPHA * score + (1 - ROLLING_ALPHA) * state.current_sentiment)
    return state.current_sentiment


def update_themes(state: CallState, themes: list[str]) -> list[str]:
    for t in themes:
        if t in state.active_themes:
            state.active_themes.remove(t)
        state.active_themes.insert(0, t)
    del state.active_themes[THEME_CAP:]
    return state.active_themes
