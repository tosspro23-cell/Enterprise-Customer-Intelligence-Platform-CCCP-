"""Post-call enrichment: turns a finished call's hot state + decisions into
the analytical-record shape described in docs/architecture.md §8 -- a
summary, a sentiment trajectory, the themes discussed, and the final
commercial outcome, ready to persist to the analytical store.

The summary is a deterministic template, not an LLM call: this slice's
narrator budget is spent on explaining the commercial decision (where an
LLM earns its place by citing approved guidance); a call summary with no
guidance to cite would just be a paraphrase, which a template does exactly
as well without the risk.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from cccp_agent import DecisionResult

from .call_state import CallState


@dataclass(frozen=True)
class EnrichmentRecord:
    call_id: str
    customer_id: str
    agent_id: str
    ended_at: str
    final_sentiment: float | None
    sentiment_trajectory: tuple[float, ...]
    themes: tuple[str, ...]
    outcome: str
    product_id: str | None
    generated_by: str | None
    degraded: tuple[str, ...]
    trace_id: str
    summary: str


def build_enrichment(state: CallState, decisions: list[DecisionResult]) -> EnrichmentRecord:
    last = decisions[-1] if decisions else None
    outcome = last.outcome.value if last else "no_decision"
    product_id = last.recommendation.product_id if last and last.recommendation else None
    generated_by = last.explanation.generated_by if last else None
    degraded = tuple(last.degraded) if last else ()

    trend_bits = []
    if state.sentiment_series:
        trend_bits.append(f"first -> last utterance sentiment: {state.sentiment_series[0]:+.2f} -> "
                          f"{state.sentiment_series[-1]:+.2f}")
    if state.active_themes:
        trend_bits.append(f"themes: {', '.join(state.active_themes)}")
    if last:
        trend_bits.append(f"commercial outcome: {outcome}" + (f" ({product_id})" if product_id else ""))
    summary = "; ".join(trend_bits) or "no signal captured"

    return EnrichmentRecord(
        call_id=state.call_id, customer_id=state.customer_id, agent_id=state.agent_id,
        ended_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        final_sentiment=state.current_sentiment, sentiment_trajectory=tuple(state.sentiment_series),
        themes=tuple(state.active_themes), outcome=outcome, product_id=product_id,
        generated_by=generated_by, degraded=degraded,
        trace_id=state.trace_id, summary=summary,
    )
