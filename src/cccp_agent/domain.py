"""Immutable domain contracts shared by every module in this package.

Everything here is a frozen dataclass: a decision result is a value, not a
live object, so it can be hashed, diffed, logged and replayed exactly as it
was produced. `AgentResult.to_dict()` is the single serialisation path used
by the CLI demo, the test suite and the eval runner, so all three observe
identical output.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date
from enum import Enum
from typing import Any


class Outcome(str, Enum):
    RECOMMENDED = "recommended"
    SUPPRESSED = "suppressed"            # hard business rule blocks commercial action (e.g. open complaint)
    DEFERRED = "deferred"                # timing rule: not now (negative live call / deteriorating relationship)
    HANDOFF = "specialist_handoff"       # vulnerable / compliance flag -> human specialist
    NO_RECOMMENDATION = "no_recommendation"
    UNAVAILABLE = "unavailable"          # a required dependency failed; nothing is fabricated


# --------------------------------------------------------------------- source data
@dataclass(frozen=True)
class Case:
    case_id: str
    case_type: str                       # "complaint" | "service_request" | ...
    status: str                          # "open" | "resolved"
    opened_at: date
    resolved_at: date | None = None


@dataclass(frozen=True)
class Customer:
    customer_id: str
    segment: str                         # "mass" | "high_value"
    region: str                          # ISO country code, e.g. "ES"
    products: tuple[str, ...]
    flags: tuple[str, ...] = ()          # e.g. "vulnerable_customer"
    cases: tuple[Case, ...] = ()


@dataclass(frozen=True)
class OfferEvent:
    product_id: str
    outcome: str                         # "accepted" | "declined" | "ignored"


@dataclass(frozen=True)
class Interaction:
    """One enriched, post-call interaction record for a customer."""
    interaction_id: str
    customer_id: str
    occurred_at: date
    channel: str
    sentiment_score: float               # [-1, 1], produced upstream by a sentiment model
    themes: tuple[str, ...]              # controlled-taxonomy tags
    summary: str                         # free text: UNTRUSTED, never forwarded to the LLM
    offers: tuple[OfferEvent, ...] = ()


@dataclass(frozen=True)
class LiveCallSignal:
    """Fast-path signal from an in-progress call, if one is active."""
    call_id: str
    current_sentiment: float             # rolling utterance-window sentiment, [-1, 1]
    active_themes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    regions: tuple[str, ...]
    segments: tuple[str, ...]
    min_propensity: float
    priority: int                        # business tie-break, lower = preferred


@dataclass(frozen=True)
class ProductScore:
    """Output of the predictive scoring service. Authoritative for propensity."""
    product_id: str
    propensity: float
    model_name: str
    model_version: str


@dataclass(frozen=True)
class GuidanceChunk:
    document_id: str
    version: str
    section: str
    effective_date: date
    product_ids: tuple[str, ...]
    situation: str                       # "post_resolution" | "standard" | "any"
    text: str
    prohibited_phrases: tuple[str, ...] = ()
    source_url: str = ""


# --------------------------------------------------------------------- derived
@dataclass(frozen=True)
class SentimentTrend:
    label: str                           # improving | stable | deteriorating | insufficient_data
    n_interactions: int
    mean: float | None
    recent_mean: float | None
    earlier_mean: float | None
    slope_per_interaction: float | None
    live_sentiment: float | None
    method: str = "sentiment-trend-v1"


@dataclass(frozen=True)
class ThemeInsight:
    theme: str
    count: int
    weighted_share: float
    recurring: bool
    emerging: bool


@dataclass(frozen=True)
class PolicyDecision:
    rule_id: str
    rule_version: str
    outcome: str                         # block | defer | handoff | exclude | allow
    subject: str                         # "customer" or a product_id
    reason: str


@dataclass(frozen=True)
class Explanation:
    text: str
    product_id: str | None
    cited_document_ids: tuple[str, ...]
    generated_by: str                    # "llm" | "template"
    prompt_profile: str | None = None
    validation_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class Evidence:
    kind: str                            # customer_record | interaction | analysis | rule | model_score | guidance_chunk | llm
    source_system: str
    source_id: str
    source_version: str | None = None
    detail: str = ""


# --------------------------------------------------------------------- request / result
@dataclass(frozen=True)
class DecisionRequest:
    customer_id: str
    as_of: date
    call_id: str | None = None
    live_signal: LiveCallSignal | None = None
    lookback_days: int = 365


@dataclass(frozen=True)
class DecisionResult:
    outcome: Outcome
    customer_id: str
    recommendation: ProductScore | None
    explanation: Explanation
    sentiment_trend: SentimentTrend
    themes: tuple[ThemeInsight, ...]
    policy_decisions: tuple[PolicyDecision, ...]
    candidates: tuple[ProductScore, ...]
    evidence: tuple[Evidence, ...]
    degraded: tuple[str, ...]
    trace_id: str
    spans: tuple[dict, ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, date):
        return obj.isoformat()
    if is_dataclass(obj):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj
