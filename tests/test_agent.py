"""Deterministic contract tests (binary correctness). Behavioural quality lives in evals/."""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cccp_agent import CommercialDecisionAgent, DecisionRequest, LiveCallSignal, Outcome  # noqa: E402
from cccp_agent.adapters.synthetic import StubNarrator, SyntheticEstate  # noqa: E402
from cccp_agent.domain import Case, Customer, Interaction, SentimentTrend  # noqa: E402
from cccp_agent.insights import analyse_sentiment_trend, identify_key_themes  # noqa: E402
from cccp_agent.narrative import allowed_numbers, validate_explanation  # noqa: E402
from cccp_agent.policy import evaluate_customer_gates, gate_outcome, situation_for  # noqa: E402

AS_OF = date(2026, 10, 5)


def _i(n, d, s, themes=()):
    return Interaction(f"i{n}", "c", date.fromisoformat(d), "voice", s, tuple(themes), "")


class TestInsights(unittest.TestCase):
    def test_improving(self):
        xs = [_i(k, d, s) for k, (d, s) in enumerate(
            [("2026-01-01", -0.6), ("2026-03-01", -0.5), ("2026-05-01", -0.4), ("2026-07-01", 0.1),
             ("2026-08-01", 0.3), ("2026-09-01", 0.4)])]
        self.assertEqual(analyse_sentiment_trend(xs, AS_OF).label, "improving")

    def test_flat_is_stable(self):
        xs = [_i(k, f"2026-0{k + 1}-01", 0.2) for k in range(5)]
        self.assertEqual(analyse_sentiment_trend(xs, AS_OF).label, "stable")

    def test_insufficient_and_window(self):
        xs = [_i(1, "2024-01-01", -0.9), _i(2, "2026-09-01", 0.1)]  # first one outside 365d window
        t = analyse_sentiment_trend(xs, AS_OF)
        self.assertEqual((t.label, t.n_interactions), ("insufficient_data", 1))

    def test_future_dated_rows_ignored(self):
        xs = [_i(1, "2026-12-01", -0.9)]
        self.assertEqual(analyse_sentiment_trend(xs, AS_OF).n_interactions, 0)

    def test_themes_recurring_emerging_live(self):
        xs = [_i(1, "2026-02-01", 0, ["fees"]), _i(2, "2026-09-20", 0, ["fees", "savings"])]
        live = LiveCallSignal("call", -0.1, ("cancellation",))
        th = {t.theme: t for t in identify_key_themes(xs, AS_OF, live)}
        self.assertTrue(th["fees"].recurring and not th["fees"].emerging)
        self.assertTrue(th["savings"].emerging and th["cancellation"].emerging)
        self.assertAlmostEqual(sum(t.weighted_share for t in th.values()), 1.0, places=2)


class TestPolicy(unittest.TestCase):
    neutral = SentimentTrend("stable", 3, 0, 0, 0, 0, None)

    def test_precedence_handoff_over_block_over_defer(self):
        c = Customer("c", "mass", "ES", (), ("vulnerable_customer",),
                     (Case("k", "complaint", "open", date(2026, 10, 1)),))
        ds = evaluate_customer_gates(c, self.neutral, LiveCallSignal("x", -0.9))
        self.assertEqual(gate_outcome(ds)[0], Outcome.HANDOFF)
        c2 = Customer("c", "mass", "ES", (), (), c.cases)
        self.assertEqual(gate_outcome(evaluate_customer_gates(c2, self.neutral, LiveCallSignal("x", -0.9)))[0],
                          Outcome.SUPPRESSED)

    def test_threshold_boundary_is_inclusive(self):
        c = Customer("c", "mass", "ES", ())
        self.assertEqual(gate_outcome(evaluate_customer_gates(c, self.neutral, LiveCallSignal("x", -0.4)))[0],
                          Outcome.DEFERRED)
        self.assertIsNone(gate_outcome(evaluate_customer_gates(c, self.neutral, LiveCallSignal("x", -0.39))))

    def test_situation(self):
        c = Customer("c", "mass", "ES", (), (), (Case("k", "complaint", "resolved", date(2026, 9, 1),
                                                        date(2026, 9, 28)),))
        self.assertEqual(situation_for(c, AS_OF), "post_resolution")
        self.assertEqual(situation_for(c, date(2026, 12, 31)), "standard")


class TestValidator(unittest.TestCase):
    def setUp(self):
        self.e = SyntheticEstate()
        self.p = self.e.catalog["savings_plus"]
        self.g = self.e.guidance_port().search_commercial_guidance("savings_plus", "post_resolution")
        self.payload = {"recommended_product": {"product_id": "savings_plus", "propensity_pct": 82,
                                                "model": "xsell-propensity:2026.09.1-synthetic"},
                        "customer_context": {"sentiment_trend_12m": "improving"},
                        "approved_guidance": [{"document_id": x.document_id, "version": x.version,
                                               "section": x.section, "text": x.text} for x in self.g],
                        "constraints": {"max_chars": 600}}

    def _v(self, raw):
        return validate_explanation(raw, product=self.p, catalog=self.e.catalog, guidance=self.g, payload=self.payload)

    def test_valid(self):
        ok = {"product_id": "savings_plus", "explanation": "Savings Plus fits (82%). Balances above 1000 EUR.",
              "cited_document_ids": ["commercial-offers-savings"]}
        self.assertEqual(self._v(ok), [])
        self.assertIn("1000", allowed_numbers(self.payload))

    def test_each_violation_detected(self):
        bad = {"product_id": "premium_card", "explanation": "Premium Card has a guaranteed return of 7%.",
               "cited_document_ids": ["made-up-doc"]}
        errs = " ".join(self._v(bad))
        for k in ("product_mismatch", "mentions_other_product", "citation_invalid", "unsupported_number",
                  "prohibited_phrase"):
            self.assertIn(k, errs)

    def test_identifiers_are_not_allowed_numbers(self):
        # Digits that only exist in key names, constraints, versions or the model id are not facts.
        self.assertEqual(allowed_numbers(self.payload), {"82", "1000"})
        for claim in ("a 12% rate", "save 600 EUR", "a 5% bonus", "earn 1% more", "five percent bonus"):
            raw = {"product_id": "savings_plus", "explanation": f"Savings Plus: {claim}.",
                   "cited_document_ids": ["commercial-offers-savings"]}
            self.assertTrue(any(e.startswith("unsupported_number") for e in self._v(raw)), claim)

    def test_identifier_references_are_not_claims(self):
        ok = {"product_id": "savings_plus", "cited_document_ids": ["commercial-offers-savings"],
              "explanation": "xsell-propensity:2026.09.1-synthetic scores Savings Plus 82% with a stable "
                             "12\u2011month trend; follow commercial-offers-savings version 5 section 2.1."}
        self.assertEqual(self._v(ok), [])

    def test_obfuscated_product_and_phrase_detected(self):
        bad = {"product_id": "savings_plus", "explanation": "Savings Plus beats the Premium-Card; no-risk.",
               "cited_document_ids": ["commercial-offers-savings"]}
        errs = " ".join(self._v(bad))
        self.assertIn("mentions_other_product: premium_card", errs)
        self.assertIn("prohibited_phrase: no risk", errs)

    def test_non_dict(self):
        self.assertTrue(self._v("not json")[0].startswith("schema"))


class TestAgentEndToEnd(unittest.TestCase):
    def setUp(self):
        self.e = SyntheticEstate()

    def _agent(self, narrator=None, model_fault=None, interactions_fault=None):
        e = self.e
        return CommercialDecisionAgent(e.customer_port(), e.interaction_port(interactions_fault),
                                        e.model_port(model_fault), e.guidance_port(), e.catalog, narrator)

    def test_golden(self):
        r = self._agent(StubNarrator()).run(DecisionRequest("cust_001", AS_OF))
        self.assertEqual(r.outcome, Outcome.RECOMMENDED)
        self.assertEqual(r.recommendation.product_id, "savings_plus")
        self.assertEqual(r.explanation.generated_by, "llm")
        self.assertTrue(r.trace_id and r.spans)
        self.assertIsInstance(r.to_dict()["outcome"], str)

    def test_no_narrator_configured_uses_template(self):
        r = self._agent(None).run(DecisionRequest("cust_001", AS_OF))
        self.assertEqual((r.outcome, r.explanation.generated_by), (Outcome.RECOMMENDED, "template"))
        self.assertIn("narrator_disabled", r.degraded)

    def test_unknown_customer_unavailable(self):
        r = self._agent(StubNarrator()).run(DecisionRequest("nope", AS_OF))
        self.assertEqual(r.outcome, Outcome.UNAVAILABLE)
        self.assertIsNone(r.recommendation)

    def test_model_error_never_fabricates(self):
        r = self._agent(StubNarrator(), "error").run(DecisionRequest("cust_001", AS_OF))
        self.assertEqual((r.outcome, r.recommendation, r.candidates), (Outcome.UNAVAILABLE, None, ()))

    def test_guidance_not_yet_effective_is_not_used(self):
        from dataclasses import replace

        class FutureGuidance:
            def __init__(self, inner):
                self._inner = inner

            def search_commercial_guidance(self, product_id, situation):
                return [replace(g, effective_date=date(2027, 1, 1))
                        for g in self._inner.search_commercial_guidance(product_id, situation)]

        e = self.e
        r = CommercialDecisionAgent(e.customer_port(), e.interaction_port(), e.model_port(),
                                    FutureGuidance(e.guidance_port()), e.catalog, StubNarrator()
                                    ).run(DecisionRequest("cust_001", AS_OF))
        self.assertEqual(r.outcome, Outcome.NO_RECOMMENDATION)
        self.assertTrue(any(d.rule_id == "G1_no_approved_guidance" for d in r.policy_decisions))

    def test_missing_history_fails_closed(self):
        # cust_009 is deferred (R3) and cust_006 has a recent decline (P4) -- both rules need history.
        for cid in ("cust_009", "cust_006", "cust_004"):
            r = self._agent(StubNarrator(), interactions_fault="error").run(DecisionRequest(cid, AS_OF))
            self.assertEqual((r.outcome, r.recommendation), (Outcome.UNAVAILABLE, None), cid)
            self.assertIn("interaction_history_unavailable", r.degraded)


class _SlowCompletions:
    def __init__(self, delay_s, content='{"x": 1}'):
        self.delay_s, self.content, self.calls = delay_s, content, 0

    def create(self, **kw):
        import time
        from types import SimpleNamespace as NS
        self.calls += 1
        time.sleep(self.delay_s)
        return NS(choices=[NS(message=NS(content=self.content))])


class TestNarratorDeadline(unittest.TestCase):
    """The narrator timeout is an end-to-end deadline: a slow provider can never hold the agent past it."""

    def _narrator(self, delay_s):
        from types import SimpleNamespace as NS
        from cccp_agent.adapters.azure_openai import AzureOpenAINarrator
        comp = _SlowCompletions(delay_s)
        return AzureOpenAINarrator(deployment="fake-rt", client=NS(chat=NS(completions=comp))), comp

    def test_slow_provider_falls_back_within_deadline(self):
        import time
        narrator, comp = self._narrator(delay_s=1.5)
        e = SyntheticEstate()
        agent = CommercialDecisionAgent(e.customer_port(), e.interaction_port(), e.model_port(), e.guidance_port(),
                                        e.catalog, narrator, narrator_timeout_s=0.3)
        t0 = time.perf_counter()
        r = agent.run(DecisionRequest("cust_001", AS_OF))
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.3 + 0.1)
        self.assertEqual((r.outcome, r.explanation.generated_by), (Outcome.RECOMMENDED, "template"))
        self.assertIn("narrator_timeout", r.degraded)
        span = next(s for s in r.spans if s["name"] == "narrator_explain")
        self.assertEqual((span["deadline_exceeded"], span["attempts"], span["deployment"]), (True, 1, "fake-rt"))
        self.assertEqual(comp.calls, 1)

    def test_fast_provider_is_not_cut_off(self):
        narrator, _ = self._narrator(delay_s=0.0)
        self.assertEqual(narrator.generate_json("p", "s", {}, 1.0), {"x": 1})
        self.assertFalse(narrator.last_call["deadline_exceeded"])

    @unittest.skipUnless(__import__("importlib").util.find_spec("openai"), "openai SDK not installed")
    def test_real_client_has_retries_disabled(self):
        import os
        from unittest import mock
        from cccp_agent.adapters.azure_openai import AzureOpenAINarrator
        env = {"AZURE_OPENAI_ENDPOINT": "https://example.invalid", "AZURE_OPENAI_API_KEY": "x",
               "AZURE_OPENAI_DEPLOYMENT": "d"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(AzureOpenAINarrator()._client.max_retries, 0)


if __name__ == "__main__":
    unittest.main()
