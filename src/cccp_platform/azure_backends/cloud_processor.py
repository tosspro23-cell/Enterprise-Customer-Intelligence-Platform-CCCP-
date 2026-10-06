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
from .sentiment_language import analyse_sentiment, tag_themes
from .speech import roundtrip as speech_roundtrip
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


def run_call_cloud_voice(
    script: dict[str, Any],
    agent: CommercialDecisionAgent,
    event_sink: EventHubSink,
    as_of: date,
) -> tuple[RedisCallState, list[DecisionResult], list[dict]]:
    """Same as `run_call_cloud`, except the script's hand-labelled `sentiment`
    and `themes` fields on each customer turn are IGNORED and recomputed for
    real: Azure TTS speaks the line, Azure STT transcribes it back, and Azure
    AI Language scores the transcribed text's sentiment. Theme tagging stays
    a keyword match (see sentiment_language.py) -- no trained classifier
    exists in this build.

    Slow by design (each customer turn costs a real TTS + STT round trip, our
    measured ~3.5s + ~6s) and bounded by the Speech/Language free-tier quota,
    so this is for a small number of demonstration calls, not the concurrent
    load test in tools/loadtest.py.

    Returns the usual (state, decisions) plus a list of per-utterance voice
    records (original vs. recognized text, measured sentiment, timings) so a
    caller can show the STT/sentiment path actually ran, not just assume it.
    """
    call_id, customer_id, agent_id = script["call_id"], script["customer_id"], script["agent_id"]
    trace_id = uuid.uuid4().hex[:16]
    seq = EventSequencer(call_id, customer_id, trace_id, "cloud-processor-voice", event_sink)
    state = RedisCallState(call_id, customer_id, agent_id)
    decisions: list[DecisionResult] = []
    voice_log: list[dict] = []

    seq.emit(CALL_STARTED, {"customer_id": customer_id, "agent_id": agent_id})

    for turn in script["utterances"]:
        seq.emit(UTTERANCE_FINAL, {"channel": turn["channel"], "text": turn["text"]})
        if turn["channel"] != "customer":
            continue

        voice = speech_roundtrip(turn["text"])
        sentiment = analyse_sentiment(voice.recognized_text or turn["text"])
        themes = tag_themes(voice.recognized_text or turn["text"])
        voice_log.append({
            "original_text": turn["text"], "recognized_text": voice.recognized_text,
            "stt_reason": voice.reason, "tts_s": voice.synthesis_s, "stt_s": voice.recognition_s,
            "sentiment": sentiment, "themes": themes,
        })

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
        })
        if result.outcome.value == "recommended":
            seq.emit(SUGGESTION_GENERATED, {
                "product_id": result.recommendation.product_id,
                "generated_by": result.explanation.generated_by,
            })

    state.end()
    seq.emit(CALL_ENDED, {})
    return state, decisions, voice_log
