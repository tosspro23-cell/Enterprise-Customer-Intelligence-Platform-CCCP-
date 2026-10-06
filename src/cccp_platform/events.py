"""The domain event envelope. In production this rides an ordered, replayable
event stream (partitioned by call id); here it is simply handed, in order,
to whatever `sink` the caller provides -- a queue for the live UI, a list for
tests, or both.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

SCHEMA_VERSION = "1.0"

# Event type constants (subset of the full catalogue in docs/architecture.md
# relevant to this reference slice).
CALL_STARTED = "call.started"
UTTERANCE_FINAL = "transcript.utterance_final"
SENTIMENT_UPDATED = "sentiment.updated"
THEME_DETECTED = "theme.detected"
DECISION_MADE = "commercial.decision_made"
SUGGESTION_GENERATED = "copilot.suggestion_generated"
CALL_ENDED = "call.ended"
POSTCALL_COMPLETED = "postcall.enrichment_completed"


@dataclass(frozen=True)
class Event:
    event_id: str
    call_id: str
    customer_ref: str
    timestamp: str
    sequence_number: int
    event_type: str
    producer: str
    trace_id: str
    schema_version: str = SCHEMA_VERSION
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventSequencer:
    """Assigns monotonically increasing sequence numbers and emits events to a sink."""

    def __init__(self, call_id: str, customer_ref: str, trace_id: str, producer: str,
                 sink: Callable[[Event], None]) -> None:
        self._call_id, self._customer_ref = call_id, customer_ref
        self._trace_id, self._producer = trace_id, producer
        self._sink = sink
        self._seq = 0

    def emit(self, event_type: str, payload: dict[str, Any]) -> Event:
        self._seq += 1
        evt = Event(
            event_id=f"evt_{uuid.uuid4().hex[:12]}",
            call_id=self._call_id,
            customer_ref=self._customer_ref,
            timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            sequence_number=self._seq,
            event_type=event_type,
            producer=self._producer,
            trace_id=self._trace_id,
            payload=payload,
        )
        self._sink(evt)
        return evt
