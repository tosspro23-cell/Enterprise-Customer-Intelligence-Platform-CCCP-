"""The Commercial Decision Agent: a bounded, auditable workflow.

The control flow below is deterministic end to end; intelligence is
delegated to whichever component owns a given decision, and nothing else:

    sentiment trend / themes  -> deterministic aggregation over scored interactions
    permission / timing       -> versioned commercial policy
    product suitability       -> the existing propensity model (authoritative)
    positioning evidence      -> approved guidance retrieval
    natural-language output   -> the narrator (LLM), validated; template fallback

This same `run()` can be called from a conversational assistant as a bounded
tool, or from a real-time call pipeline with a `LiveCallSignal` attached to
the request -- the decision logic does not change with the caller.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from contextlib import contextmanager
from datetime import timedelta
from typing import Iterator

from .domain import (DecisionRequest, DecisionResult, Evidence, Explanation, Outcome, PolicyDecision, Product,
                      ProductScore, SentimentTrend)
from .insights import THEME_METHOD, analyse_sentiment_trend, identify_key_themes
from .narrative import (PROMPT_PROFILE, SYSTEM_PROMPT, build_narrator_payload, non_offer_explanation,
                         template_explanation, validate_explanation)
from .policy import (POLICY_VERSION, PolicyConfig, evaluate_customer_gates, evaluate_product, gate_outcome,
                      situation_for)
from .ports import (CustomerDirectoryPort, DependencyError, DependencyTimeout, GuidanceIndexPort,
                     InteractionHistoryPort, NarratorPort, PropensityModelPort)


class _Trace:
    def __init__(self) -> None:
        self.trace_id = uuid.uuid4().hex[:16]
        self.spans: list[dict] = []

    @contextmanager
    def span(self, name: str, **attrs) -> Iterator[dict]:
        rec = {"name": name, **attrs}
        t0 = time.perf_counter()
        try:
            yield rec
            rec.setdefault("status", "ok")
        except Exception as e:  # recorded, then re-raised to the caller's handler
            rec["status"] = f"error:{type(e).__name__}"
            raise
        finally:
            rec["duration_ms"] = round((time.perf_counter() - t0) * 1000, 2)
            self.spans.append(rec)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


class CommercialDecisionAgent:
    def __init__(
        self,
        customers: CustomerDirectoryPort,
        interactions: InteractionHistoryPort,
        model: PropensityModelPort,
        guidance: GuidanceIndexPort,
        catalog: dict[str, Product],
        narrator: NarratorPort | None = None,
        policy: PolicyConfig = PolicyConfig(),
        narrator_timeout_s: float = 2.5,
    ) -> None:
        self.customers, self.interactions, self.model = customers, interactions, model
        self.guidance, self.catalog, self.narrator = guidance, catalog, narrator
        self.policy, self.narrator_timeout_s = policy, narrator_timeout_s

    # ------------------------------------------------------------------ main flow
    def run(self, req: DecisionRequest) -> DecisionResult:
        tr = _Trace()
        evidence: list[Evidence] = []
        degraded: list[str] = []
        decisions: list[PolicyDecision] = []

        def result(outcome: Outcome, explanation: Explanation, trend: SentimentTrend, themes=(),
                    rec: ProductScore | None = None, candidates=()) -> DecisionResult:
            return DecisionResult(outcome, req.customer_id, rec, explanation, trend, tuple(themes),
                                   tuple(decisions), tuple(candidates), tuple(evidence), tuple(degraded),
                                   tr.trace_id, tuple(tr.spans))

        # 1. Customer context (required)
        try:
            with tr.span("get_customer"):
                customer = self.customers.get_customer(req.customer_id)
        except DependencyError as e:
            degraded.append("customer_data_unavailable")
            empty = SentimentTrend("insufficient_data", 0, None, None, None, None, None)
            return result(Outcome.UNAVAILABLE, non_offer_explanation(str(e) or "customer data unavailable",
                                                                      "D1_dependency"), empty)
        evidence.append(Evidence("customer_record", "customer_directory", customer.customer_id))

        # 2. Interaction history (required: R3 deteriorating-relationship and P4
        # decline-cooldown both read it, so an empty stand-in would silently
        # disable them -- fail closed, never offer on missing history)
        since = req.as_of - timedelta(days=req.lookback_days)
        try:
            with tr.span("get_interactions") as s:
                history = self.interactions.get_interactions(customer.customer_id, since)
                s["n"] = len(history)
        except DependencyError:
            degraded.append("interaction_history_unavailable")
            empty = SentimentTrend("insufficient_data", 0, None, None, None, None,
                                   req.live_signal.current_sentiment if req.live_signal else None)
            return result(Outcome.UNAVAILABLE,
                           non_offer_explanation("interaction history unavailable; history-based policy rules "
                                                  "cannot be evaluated", "D4_history_unavailable"), empty)
        evidence += [Evidence("interaction", "interaction_history", i.interaction_id) for i in history]

        # 3. Deterministic analytics: sentiment trend + key themes
        with tr.span("analyse"):
            trend = analyse_sentiment_trend(history, req.as_of, req.live_signal, req.lookback_days)
            themes = identify_key_themes(history, req.as_of, req.live_signal, req.lookback_days)
        evidence.append(Evidence("analysis", "cccp.insights", trend.method, THEME_METHOD))

        # 4. Customer-level gates BEFORE scoring (cheap, deterministic, most restrictive wins)
        with tr.span("customer_gates"):
            decisions += evaluate_customer_gates(customer, trend, req.live_signal, self.policy)
            gate = gate_outcome(decisions)
        evidence.append(Evidence("rule", "cccp.policy", POLICY_VERSION))
        if gate:
            outcome, d = gate
            return result(outcome, non_offer_explanation(d.reason, d.rule_id), trend, themes)

        # 5. Existing propensity model (authoritative for suitability)
        try:
            with tr.span("score_products") as s:
                scores = self.model.score_products(customer.customer_id)
                s["n"] = len(scores)
        except DependencyError as e:
            degraded.append("model_timeout" if isinstance(e, DependencyTimeout) else "model_unavailable")
            return result(Outcome.UNAVAILABLE,
                           non_offer_explanation("recommendation model unavailable; no score is fabricated",
                                                  "D2_model_unavailable"), trend, themes)
        evidence += [Evidence("model_score", s.model_name, s.product_id, s.model_version, f"{s.propensity:.2f}")
                     for s in scores]

        # 6. Product-level eligibility, rank by model score (business priority breaks ties)
        eligible: list[ProductScore] = []
        for sc in scores:
            d = evaluate_product(sc, customer, history, self.catalog, req.as_of, self.policy)
            decisions.append(d)
            if d.outcome == "allow":
                eligible.append(sc)
        eligible.sort(key=lambda s: (-s.propensity, self.catalog[s.product_id].priority))

        # 7. Approved guidance is REQUIRED to position an offer
        situation = situation_for(customer, req.as_of, self.policy)
        chosen: ProductScore | None = None
        guidance = []
        for cand in eligible:
            try:
                with tr.span("search_guidance", product_id=cand.product_id, situation=situation):
                    guidance = self.guidance.search_commercial_guidance(cand.product_id, situation)
                    # Guidance not yet in force is not approved guidance (enforced here, not
                    # trusted to each index adapter).
                    guidance = [g for g in guidance if g.effective_date <= req.as_of]
            except DependencyError:
                degraded.append("guidance_unavailable")
                return result(Outcome.UNAVAILABLE,
                               non_offer_explanation("guidance index unavailable; no policy claim made",
                                                      "D3_guidance_unavailable"), trend, themes, candidates=scores)
            if guidance:
                chosen = cand
                break
            decisions.append(PolicyDecision("G1_no_approved_guidance", POLICY_VERSION, "exclude", cand.product_id,
                                             "no approved commercial guidance for this product/situation"))
        if chosen is None:
            return result(Outcome.NO_RECOMMENDATION,
                           non_offer_explanation("no eligible product with approved guidance", "N1_none_eligible"),
                           trend, themes, candidates=scores)
        evidence += [Evidence("guidance_chunk", "guidance_index", g.document_id, g.version,
                               f"section {g.section}; hash {_hash(g.text)}") for g in guidance]

        # 8. Narrator explanation (validated) with deterministic fallback
        product = self.catalog[chosen.product_id]
        explanation = self._explain(product, chosen, trend, themes, guidance, situation, tr, degraded, evidence)
        return result(Outcome.RECOMMENDED, explanation, trend, themes, rec=chosen, candidates=scores)

    # ------------------------------------------------------------------ explanation
    def _explain(self, product, score, trend, themes, guidance, situation, tr, degraded, evidence) -> Explanation:
        if self.narrator is None:
            degraded.append("narrator_disabled")
            return template_explanation(product, score, trend, themes, guidance)
        payload = build_narrator_payload(product, score, trend, themes, guidance, situation)
        try:
            with tr.span("narrator_explain", profile=PROMPT_PROFILE, deadline_s=self.narrator_timeout_s) as s:
                try:
                    raw = self.narrator.generate_json(PROMPT_PROFILE, SYSTEM_PROMPT, payload, self.narrator_timeout_s)
                finally:  # adapter-reported call facts (deployment, attempts, deadline_exceeded), if any
                    s.update(getattr(self.narrator, "last_call", None) or {})
        except DependencyError as e:
            degraded.append("narrator_timeout" if isinstance(e, DependencyTimeout) else "narrator_error")
            return template_explanation(product, score, trend, themes, guidance)
        errors = validate_explanation(raw, product=product, catalog=self.catalog, guidance=guidance, payload=payload)
        evidence.append(Evidence("llm", "narrator", PROMPT_PROFILE, None,
                                  "valid" if not errors else f"rejected: {len(errors)} error(s)"))
        if errors:
            degraded.append("narrator_output_rejected")
            return template_explanation(product, score, trend, themes, guidance, tuple(errors))
        return Explanation(raw["explanation"], product.product_id, tuple(raw["cited_document_ids"]), "llm",
                            PROMPT_PROFILE)
