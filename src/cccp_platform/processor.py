"""The stream processor: consumes a scripted call turn by turn, updates hot
state, and calls the real `CommercialDecisionAgent` on each triggered turn
-- exactly the role module M5-M10 play in the production design, minus the
network hops.

The script decides *when* a turn is a trigger (an explicit `trigger: true`
flag per scripted turn), standing in for the production trigger policy
(M8: new high-priority theme, a sentiment threshold crossed, an explicit
agent request, ...). What happens once triggered -- building a
`LiveCallSignal`, calling the agent, handling its outcome -- is identical to
the real-time path.
"""
from __future__ import annotations

import time
import uuid
from datetime import date
from typing import Any, Callable

from cccp_agent import CommercialDecisionAgent, DecisionRequest, DecisionResult, LiveCallSignal

from .call_state import CallState, update_sentiment, update_themes
from .events import (CALL_ENDED, CALL_STARTED, DECISION_MADE, EventSequencer, SENTIMENT_UPDATED,
                      SUGGESTION_GENERATED, THEME_DETECTED, UTTERANCE_FINAL)

TURN_DELAY_S = 0.55   # paced for a human to follow in the demo UI; not a latency claim


def run_call(
    script: dict[str, Any],
    agent: CommercialDecisionAgent,
    sink: Callable[[Any], None],
    as_of: date,
    pace: bool = True,
    customer_profile: dict[str, Any] | None = None,
) -> tuple[CallState, list[DecisionResult]]:
    """Runs one scripted call end to end, emitting events to `sink` as it goes.

    `customer_profile` is presentation-only (name, segment, region, products,
    flags) for the Workbench to show who's on the call -- the caller builds
    it from whatever customer directory it has; this module doesn't know
    about that directory's implementation.

    Returns the final hot-call state and every DecisionResult produced
    during the call, for the post-call handoff.
    """
    call_id, customer_id, agent_id = script["call_id"], script["customer_id"], script["agent_id"]
    trace_id = uuid.uuid4().hex[:16]
    seq = EventSequencer(call_id, customer_id, trace_id, "stream-processor-sim", sink)
    state = CallState(call_id, customer_id, agent_id, trace_id=trace_id)
    decisions: list[DecisionResult] = []

    started_payload = {"customer_id": customer_id, "agent_id": agent_id}
    if customer_profile:
        started_payload["customer_profile"] = customer_profile
    seq.emit(CALL_STARTED, started_payload)

    for turn in script["utterances"]:
        if pace:
            time.sleep(TURN_DELAY_S)
        seq.emit(UTTERANCE_FINAL, {"channel": turn["channel"], "text": turn["text"]})

        if turn["channel"] != "customer":
            continue
        sentiment, themes = turn.get("sentiment"), turn.get("themes", [])
        if sentiment is not None:
            rolling = update_sentiment(state, sentiment)
            seq.emit(SENTIMENT_UPDATED, {"utterance_sentiment": sentiment, "rolling_sentiment": round(rolling, 3)})
        if themes:
            active = update_themes(state, themes)
            seq.emit(THEME_DETECTED, {"themes": themes, "active_themes": active})

        if not turn.get("trigger"):
            continue

        live = LiveCallSignal(call_id, state.current_sentiment or 0.0, tuple(state.active_themes))
        result = agent.run(DecisionRequest(customer_id, as_of, call_id=call_id, live_signal=live))
        decisions.append(result)
        state.commercial_state = result.outcome.value
        seq.emit(DECISION_MADE, {
            "outcome": result.outcome.value,
            "policy_decisions": [d.rule_id for d in result.policy_decisions],
            "degraded": list(result.degraded),
            "trace_id": result.trace_id,
        })
        if result.outcome.value == "recommended":
            seq.emit(SUGGESTION_GENERATED, {
                "product_id": result.recommendation.product_id,
                "propensity": result.recommendation.propensity,
                "explanation": result.explanation.text,
                "cited_document_ids": list(result.explanation.cited_document_ids),
                "generated_by": result.explanation.generated_by,
                "evidence": [e.__dict__ for e in result.evidence],
            })

    state.status = "ended"
    seq.emit(CALL_ENDED, {})
    return state, decisions
