"""Contract tests for the reference-slice platform code (cccp_platform).

These check the simulator/post-call/store/assistant wiring in isolation from
the Workbench server -- the behaviour that actually matters for the demo:
the right decisions get triggered at the right points, the right facts get
persisted, and the assistant answers from what was actually persisted.
"""
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cccp_agent import CommercialDecisionAgent  # noqa: E402
from cccp_agent.adapters.synthetic import StubNarrator, SyntheticEstate  # noqa: E402
from cccp_platform import assistant as assistant_mod  # noqa: E402
from cccp_platform.call_state import CallState, update_sentiment, update_themes  # noqa: E402
from cccp_platform.postcall import build_enrichment  # noqa: E402
from cccp_platform.processor import run_call  # noqa: E402
from cccp_platform.store import AnalyticalStore  # noqa: E402

AS_OF = date(2026, 10, 5)
CALLS_DIR = ROOT / "data" / "calls"


def _agent() -> CommercialDecisionAgent:
    e = SyntheticEstate()
    return CommercialDecisionAgent(e.customer_port(), e.interaction_port(), e.model_port(),
                                    e.guidance_port(), e.catalog, StubNarrator())


def _load(name: str) -> dict:
    return json.loads((CALLS_DIR / name).read_text())


class TestCallState(unittest.TestCase):
    def test_rolling_sentiment_is_bounded_by_inputs(self):
        s = CallState("c", "cust", "agt")
        update_sentiment(s, -0.6)
        update_sentiment(s, 0.4)
        self.assertEqual(s.sentiment_series, [-0.6, 0.4])
        self.assertTrue(-0.6 < s.current_sentiment < 0.4)

    def test_theme_recency_and_cap(self):
        s = CallState("c", "cust", "agt")
        for themes in (["a"], ["b"], ["a", "c"], ["d"], ["e"], ["f"]):
            update_themes(s, themes)
        self.assertEqual(len(s.active_themes), 5)
        self.assertEqual(s.active_themes[0], "f")   # most recent first
        self.assertNotIn("b", s.active_themes)      # evicted by the cap


class TestRunCall(unittest.TestCase):
    def test_golden_script_defers_then_recommends(self):
        script = _load("golden_savings.json")
        events = []
        state, decisions = run_call(script, _agent(), events.append, AS_OF, pace=False)
        outcomes = [d.outcome.value for d in decisions]
        self.assertEqual(outcomes, ["deferred", "recommended"])
        self.assertEqual(decisions[-1].recommendation.product_id, "savings_plus")
        self.assertEqual(state.status, "ended")
        event_types = [e.event_type for e in events]
        self.assertIn("copilot.suggestion_generated", event_types)
        # sequence numbers are strictly increasing and gapless
        seqs = [e.sequence_number for e in events]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))

    def test_suppressed_script_never_calls_the_model(self):
        script = _load("suppressed_complaint.json")
        events = []
        agent = _agent()
        _, decisions = run_call(script, agent, events.append, AS_OF, pace=False)
        self.assertTrue(all(d.outcome.value == "suppressed" for d in decisions))
        self.assertTrue(all(d.recommendation is None for d in decisions))
        self.assertEqual(agent.model.calls, 0)
        # copilot.suggestion_generated now fires for every outcome (it also
        # carries the non-commercial "focus on service recovery" guidance an
        # agent should see on a suppressed call) -- the actual compliance
        # guarantee is that it never names a product here.
        suggestions = [e.payload for e in events if e.event_type == "copilot.suggestion_generated"]
        self.assertTrue(suggestions)
        self.assertTrue(all(s["product_id"] is None for s in suggestions))


# Every scripted call in data/calls/, and the outcome/product it's supposed to
# demonstrate. Guards against the underlying synthetic fixtures (customers,
# model scores, guidance) drifting out from under a scenario without anyone
# noticing the Workbench demo for it silently stopped proving what its title
# claims.
SCENARIO_EXPECTATIONS = {
    "golden_savings.json": ("recommended", "savings_plus"),
    "suppressed_complaint.json": ("suppressed", None),
    "vulnerable_handoff.json": ("specialist_handoff", None),
    "deteriorating_trend.json": ("deferred", None),
    "already_held_switch.json": ("recommended", "premium_card"),
    "region_restriction.json": ("recommended", "premium_card"),
    "recent_decline_cooldown.json": ("recommended", "travel_insurance"),
    "below_threshold.json": ("no_recommendation", None),
    "no_history.json": ("recommended", "savings_plus"),
    "injection_safety.json": ("recommended", "savings_plus"),
    "unknown_and_missing_guidance.json": ("recommended", "savings_plus"),
}


class TestAllScenarios(unittest.TestCase):
    def test_every_call_script_is_covered_by_an_expectation(self):
        on_disk = {p.name for p in CALLS_DIR.glob("*.json")}
        self.assertEqual(on_disk, set(SCENARIO_EXPECTATIONS),
                          "a script was added/removed in data/calls/ without updating SCENARIO_EXPECTATIONS")

    def test_each_scenario_produces_its_documented_outcome(self):
        for filename, (expected_outcome, expected_product) in SCENARIO_EXPECTATIONS.items():
            with self.subTest(filename=filename):
                script = _load(filename)
                _, decisions = run_call(script, _agent(), lambda e: None, AS_OF, pace=False)
                self.assertTrue(decisions, f"{filename}: no decision was triggered at all")
                last = decisions[-1]
                self.assertEqual(last.outcome.value, expected_outcome, filename)
                product = last.recommendation.product_id if last.recommendation else None
                self.assertEqual(product, expected_product, filename)


class TestPostcallAndStore(unittest.TestCase):
    def test_enrichment_and_persistence_round_trip(self):
        script = _load("golden_savings.json")
        state, decisions = run_call(script, _agent(), lambda e: None, AS_OF, pace=False)
        rec = build_enrichment(state, decisions)
        self.assertEqual(rec.outcome, "recommended")
        self.assertEqual(rec.product_id, "savings_plus")

        with tempfile.TemporaryDirectory() as d:
            store = AnalyticalStore(Path(d) / "test.db")
            store.insert_call(rec)
            self.assertEqual(store.outcome_counts(), {"recommended": 1})
            self.assertIsNotNone(store.avg_final_sentiment())
            self.assertEqual(len(store.recent_calls()), 1)

    def test_no_decisions_on_a_call_yields_no_decision_outcome(self):
        state = CallState("c", "cust_001", "agt")
        state.status = "ended"
        rec = build_enrichment(state, [])
        self.assertEqual(rec.outcome, "no_decision")
        self.assertIsNone(rec.product_id)


class TestAssistant(unittest.TestCase):
    def setUp(self):
        self.estate = SyntheticEstate()
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AnalyticalStore(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def test_metrics_question_with_no_calls_yet(self):
        a = assistant_mod.ask("how many calls were suppressed?", self.store,
                               self.estate.guidance_chunks, self.estate.catalog)
        self.assertEqual(a.tool, "metrics.query")
        self.assertIn("No calls recorded", a.answer)

    def test_metrics_question_after_a_call(self):
        state, decisions = run_call(_load("suppressed_complaint.json"), _agent(), lambda e: None, AS_OF, pace=False)
        self.store.insert_call(build_enrichment(state, decisions))
        a = assistant_mod.ask("how many calls were suppressed?", self.store,
                               self.estate.guidance_chunks, self.estate.catalog)
        self.assertIn("suppressed: 1", a.answer)

    def test_guidance_question_cites_a_document(self):
        a = assistant_mod.ask("what can I say about Savings Plus?", self.store,
                               self.estate.guidance_chunks, self.estate.catalog)
        self.assertEqual(a.tool, "guidance.search")
        self.assertIn("commercial-offers-savings", a.citations)

    def test_unroutable_question_names_its_own_limits(self):
        a = assistant_mod.ask("what's the weather like today?", self.store,
                               self.estate.guidance_chunks, self.estate.catalog)
        self.assertEqual(a.tool, "none")


class TestTextMetrics(unittest.TestCase):
    def test_word_error_rate(self):
        from cccp_platform.text_metrics import word_error_rate
        self.assertEqual(word_error_rate("I want to save money", "i want to save money."), 0.0)
        self.assertEqual(word_error_rate("one two three four", "one three four five"), 0.5)  # 1 del + 1 ins
        self.assertEqual(word_error_rate("hello there", ""), 1.0)
        self.assertIsNone(word_error_rate("", "anything"))


class TestTriggerEngine(unittest.TestCase):
    """Unit-level coverage of trigger.py in isolation -- TestAllScenarios
    above already covers it end to end against all 11 scripts, but these
    pin the specific edge/cooldown/fallback semantics directly so a future
    change to the policy shows exactly which rule broke."""

    def setUp(self):
        from cccp_platform.trigger import TriggerEngine
        self.engine = TriggerEngine()

    def test_negative_sentiment_edge_fires_once(self):
        self.assertEqual(self.engine.evaluate(-0.6, []), "sentiment_crossed_negative_threshold")
        # staying negative on later turns must not re-fire the same edge
        self.assertIsNone(self.engine.evaluate(-0.5, []))
        self.assertIsNone(self.engine.evaluate(-0.7, []))

    def test_recovery_only_fires_after_a_genuine_negative_dip(self):
        from cccp_platform.trigger import TriggerEngine
        # never dipped negative -> "recovered" has nothing to recover from.
        # A high fallback isolates that from the separate periodic-check
        # behaviour (covered in its own test below).
        engine = TriggerEngine(fallback_every_turns=100)
        self.assertIsNone(engine.evaluate(0.05, []))
        self.assertIsNone(engine.evaluate(0.2, []))

    def test_recovery_fires_once_after_negative(self):
        self.engine.evaluate(-0.5, [])               # dip
        self.assertIsNone(self.engine.evaluate(-0.1, []))   # still below recovery_threshold
        self.assertEqual(self.engine.evaluate(0.15, []), "sentiment_recovered")
        self.assertIsNone(self.engine.evaluate(0.3, []))    # already recovered once

    def test_each_new_theme_fires_once_but_a_repeat_does_not(self):
        self.assertEqual(self.engine.evaluate(None, ["savings"]), "new_theme:savings")
        self.assertIsNone(self.engine.evaluate(None, ["savings"]))  # still active, not new
        self.assertEqual(self.engine.evaluate(None, ["savings", "fees"]), "new_theme:fees")

    def test_theme_bookkeeping_happens_even_when_sentiment_explains_the_turn(self):
        # Regression: an early return from the sentiment branch used to skip
        # updating _seen_themes, so a theme merely still active from an
        # earlier turn (active_themes is a rolling window) would wrongly
        # look "new" again later and fire a second time.
        self.assertEqual(self.engine.evaluate(-0.6, ["fees"]), "sentiment_crossed_negative_threshold")
        self.assertIsNone(self.engine.evaluate(-0.5, ["fees"]))  # "fees" must already be "seen"

    def test_periodic_fallback_fires_when_nothing_else_does(self):
        engine = type(self.engine)(fallback_every_turns=2)
        self.assertIsNone(engine.evaluate(0.1, []))
        self.assertEqual(engine.evaluate(0.1, []), "periodic_check")

    def test_periodic_fallback_is_one_shot_not_a_recurring_timer(self):
        # Once the call has been assessed at least once (by any condition),
        # further periodic checks would spend a real decision call on a
        # conversation that hasn't actually changed -- only a genuine edge
        # should trigger again after that.
        engine = type(self.engine)(fallback_every_turns=2)
        engine.evaluate(0.1, [])
        self.assertEqual(engine.evaluate(0.1, []), "periodic_check")
        for _ in range(6):
            self.assertIsNone(engine.evaluate(0.1, []))

    def test_describe_covers_every_reason_shape(self):
        from cccp_platform.trigger import describe
        for reason in ("sentiment_crossed_negative_threshold", "sentiment_recovered",
                       "periodic_check", "new_theme:savings"):
            self.assertNotEqual(describe(reason), reason)  # every known shape gets a real gloss
            self.assertTrue(describe(reason))


if __name__ == "__main__":
    unittest.main()
