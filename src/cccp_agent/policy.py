"""Deterministic commercial policy.

Policy decides permission and timing; the propensity model decides product
suitability; the narrator (LLM) decides neither. The rule set is versioned
and every decision it produces carries that version into the trace, so a
decision made last month can be re-explained exactly as it was made.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .domain import Customer, Interaction, LiveCallSignal, Outcome, PolicyDecision, Product, ProductScore, SentimentTrend

POLICY_VERSION = "commercial-policy-v1.0"


@dataclass(frozen=True)
class PolicyConfig:
    live_negative_threshold: float = -0.4
    decline_cooldown_days: int = 90
    handoff_flags: tuple[str, ...] = ("vulnerable_customer", "compliance_review")
    post_resolution_window_days: int = 30


def _decision(rule_id: str, outcome: str, subject: str, reason: str) -> PolicyDecision:
    return PolicyDecision(rule_id, POLICY_VERSION, outcome, subject, reason)


# --------------------------------------------------------- customer-level gates (run BEFORE scoring)
def evaluate_customer_gates(
    customer: Customer,
    trend: SentimentTrend,
    live: LiveCallSignal | None,
    cfg: PolicyConfig = PolicyConfig(),
) -> list[PolicyDecision]:
    out: list[PolicyDecision] = []
    flags = sorted(set(customer.flags) & set(cfg.handoff_flags))
    if flags:
        out.append(_decision("R0_handoff_flag", "handoff", "customer", f"customer flagged: {', '.join(flags)}"))
    open_complaints = [c for c in customer.cases if c.case_type == "complaint" and c.status == "open"]
    if open_complaints:
        ids = ", ".join(c.case_id for c in open_complaints)
        out.append(_decision("R1_open_complaint", "block", "customer", f"open complaint case(s): {ids}"))
    if live is not None and live.current_sentiment <= cfg.live_negative_threshold:
        out.append(_decision("R2_live_negative_sentiment", "defer", "customer",
                              f"live sentiment {live.current_sentiment:+.2f} <= {cfg.live_negative_threshold:+.2f}"))
    if trend.label == "deteriorating":
        out.append(_decision("R3_deteriorating_relationship", "defer", "customer",
                              "12-month sentiment trend deteriorating: retention before cross-sell"))
    return out


_PRECEDENCE = (("handoff", Outcome.HANDOFF), ("block", Outcome.SUPPRESSED), ("defer", Outcome.DEFERRED))


def gate_outcome(decisions: list[PolicyDecision]) -> tuple[Outcome, PolicyDecision] | None:
    """Most restrictive gate wins: handoff > block > defer."""
    for key, outcome in _PRECEDENCE:
        for d in decisions:
            if d.outcome == key:
                return outcome, d
    return None


def situation_for(customer: Customer, as_of: date, cfg: PolicyConfig = PolicyConfig()) -> str:
    for c in customer.cases:
        if (c.case_type == "complaint" and c.status == "resolved" and c.resolved_at
                and 0 <= (as_of - c.resolved_at).days <= cfg.post_resolution_window_days):
            return "post_resolution"
    return "standard"


# --------------------------------------------------------- product-level eligibility (run AFTER scoring)
def evaluate_product(
    score: ProductScore,
    customer: Customer,
    interactions: list[Interaction],
    catalog: dict[str, Product],
    as_of: date,
    cfg: PolicyConfig = PolicyConfig(),
) -> PolicyDecision:
    pid = score.product_id
    product = catalog.get(pid)
    if product is None:
        return _decision("P0_unknown_product", "exclude", pid, "model returned a product not in the approved catalog")
    if pid in customer.products:
        return _decision("P1_already_held", "exclude", pid, "customer already holds product")
    if customer.region not in product.regions:
        return _decision("P2_region_unavailable", "exclude", pid, f"not offered in {customer.region}")
    if customer.segment not in product.segments:
        return _decision("P3_segment_ineligible", "exclude", pid, f"segment {customer.segment} not eligible")
    for i in interactions:
        age = (as_of - i.occurred_at).days
        if 0 <= age <= cfg.decline_cooldown_days and any(
                o.product_id == pid and o.outcome == "declined" for o in i.offers):
            return _decision("P4_recent_decline", "exclude", pid,
                              f"declined {age} days ago (cooldown {cfg.decline_cooldown_days}d)")
    if score.propensity < product.min_propensity:
        return _decision("P5_below_threshold", "exclude", pid,
                          f"propensity {score.propensity:.2f} < {product.min_propensity:.2f}")
    return _decision("P_eligible", "allow", pid, "all eligibility rules passed")
