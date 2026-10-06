"""A minimal supervisor assistant: one router, two typed read-only tools.

Production (docs/architecture.md §9) is a hosted LLM router planning across
several specialist agents. This is deliberately not that -- it is a
keyword router dispatching to `metrics.query` and `guidance.search`, so the
demo can show the *shape* (typed tools, no free-form SQL, every answer
carries its source) without presenting a toy classifier as a validated
language-understanding router. Every answer says so.
"""
from __future__ import annotations

from dataclasses import dataclass

from cccp_agent.domain import GuidanceChunk, Product

from .store import AnalyticalStore

ROUTER_NOTE = "answered by a keyword-matched demo router, not a production intent classifier"


@dataclass(frozen=True)
class AssistantAnswer:
    answer: str
    tool: str
    source: str
    citations: tuple[str, ...] = ()


def metrics_query(store: AnalyticalStore) -> AssistantAnswer:
    counts = store.outcome_counts()
    total = sum(counts.values())
    avg = store.avg_final_sentiment()
    if not total:
        return AssistantAnswer("No calls recorded yet in this session.", "metrics.query",
                                "call_enrichment_fact")
    parts = [f"{outcome}: {n}" for outcome, n in sorted(counts.items())]
    avg_txt = f"{avg:+.2f}" if avg is not None else "n/a"
    return AssistantAnswer(
        f"{total} call(s) this session -- {', '.join(parts)}. Average final sentiment: {avg_txt}.",
        "metrics.query", "call_enrichment_fact",
    )


def guidance_search(question: str, guidance: list[GuidanceChunk], catalog: dict[str, Product]) -> AssistantAnswer:
    low = question.lower()
    hit_product = next((p for p in catalog.values() if p.name.lower() in low or p.product_id in low), None)
    chunks = [g for g in guidance if hit_product is None or hit_product.product_id in g.product_ids]
    if not chunks:
        return AssistantAnswer("No approved guidance matched that question.", "guidance.search", "guidance_index")
    g = chunks[0]
    return AssistantAnswer(
        f"Per {g.document_id} v{g.version} section {g.section}: {g.text}",
        "guidance.search", "guidance_index", (g.document_id,),
    )


_METRIC_WORDS = ("how many", "count", "average", "avg", "metric", "sentiment", "recommended", "suppressed",
                  "deferred", "handoff", "outcome", "acceptance")
_GUIDANCE_WORDS = ("guidance", "policy", "allowed to say", "approved", "position", "fee", "savings", "card",
                    "insurance", "advisory")


def ask(question: str, store: AnalyticalStore, guidance: list[GuidanceChunk],
        catalog: dict[str, Product]) -> AssistantAnswer:
    low = question.lower()
    if any(w in low for w in _METRIC_WORDS):
        a = metrics_query(store)
    elif any(w in low for w in _GUIDANCE_WORDS):
        a = guidance_search(question, guidance, catalog)
    else:
        a = AssistantAnswer(
            "I can answer questions about this session's call outcomes (e.g. \"how many calls were "
            "suppressed?\", \"average sentiment?\") or approved guidance for a product (e.g. \"what can I "
            "say about Savings Plus?\").", "none", "router")
    return AssistantAnswer(a.answer, a.tool, a.source, a.citations)
