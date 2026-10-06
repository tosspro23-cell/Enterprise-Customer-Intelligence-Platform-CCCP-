"""The same scripted-call simulation as `cccp_platform.processor.run_call`,
except hot state is a real Redis round trip and the event bus is a real
Event Hub publish, instead of an in-process dict and queue. This is what
the load test in `loadtest.py` actually drives.

Intentionally separate from `processor.py` rather than a shared abstraction:
the local Workbench demo must keep working with zero Azure dependencies,
and bending its types to also fit a Redis/Event Hubs backend would obscure
both. The decision logic underneath (`CommercialDecisionAgent.run`) is the
one thing both paths share unmodified.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from cccp_agent import CommercialDecisionAgent, DecisionRequest, DecisionResult, LiveCallSignal

from .event_hub_bus import EventHubSink
from .redis_state import RedisCallState
from ..events import (CALL_ENDED, CALL_STARTED, DECISION_MADE, EventSequencer, SENTIMENT_UPDATED,
                       SUGGESTION_GENERATED, THEME_DETECTED, UTTERANCE_FINAL)


def run_call_cloud(
    script: dict[str, Any],
    agent: CommercialDecisionAgent,
    event_sink: EventHubSink,
    as_of: date,
) -> tuple[RedisCallState, list[DecisionResult]]:
    call_id, customer_id, agent_id = script["call_id"], script["customer_id"], script["agent_id"]
    trace_id = uuid.uuid4().hex[:16]
    seq = EventSequencer(call_id, customer_id, trace_id, "cloud-processor", event_sink)
    state = RedisCallState(call_id, customer_id, agent_id)
    decisions: list[DecisionResult] = []

    seq.emit(CALL_STARTED, {"customer_id": customer_id, "agent_id": agent_id})

    for turn in script["utterances"]:
        seq.emit(UTTERANCE_FINAL, {"channel": turn["channel"], "text": turn["text"]})
        if turn["channel"] != "customer":
            continue
        sentiment, themes = turn.get("sentiment"), turn.get("themes", [])
        if sentiment is not None:
            rolling = state.update_sentiment(sentiment)
            seq.emit(SENTIMENT_UPDATED, {"utterance_sentiment": sentiment, "rolling_sentiment": round(rolling, 3)})
        if themes:
            active = state.update_themes(themes)
            seq.emit(THEME_DETECTED, {"themes": themes, "active_themes": active})
        if not turn.get("trigger"):
            continue

        live = LiveCallSignal(call_id, state.current_sentiment or 0.0, tuple(state.active_themes))
        result = agent.run(DecisionRequest(customer_id, as_of, call_id=call_id, live_signal=live))
        decisions.append(result)
        state.set_commercial_state(result.outcome.value)
        seq.emit(DECISION_MADE, {
            "outcome": result.outcome.value,
            "policy_decisions": [d.rule_id for d in result.policy_decisions],
            "degraded": list(result.degraded),
            "trace_id": result.trace_id,
            "spans": [{"name": s["name"], "duration_ms": s["duration_ms"]} for s in result.spans],
        })
        if result.outcome.value == "recommended":
            seq.emit(SUGGESTION_GENERATED, {
                "product_id": result.recommendation.product_id,
                "generated_by": result.explanation.generated_by,
            })

    state.end()
    seq.emit(CALL_ENDED, {})
    return state, decisions
