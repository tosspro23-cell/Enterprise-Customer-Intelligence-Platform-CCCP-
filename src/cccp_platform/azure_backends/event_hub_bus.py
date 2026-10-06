"""The domain event stream, backed by a real Event Hub instead of the
in-process `queue.Queue` the local Workbench demo uses. Partition key is
`call_id`, matching docs/architecture.md §8/§9.4 exactly -- all of one
call's events land on the same partition, so a consumer reading one
partition sees them in order.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass

from azure.eventhub import EventData, EventHubConsumerClient, EventHubProducerClient


def _connection_str() -> str:
    return os.environ["EVENTHUB_CONNECTION_STRING"]


def _hub_name() -> str:
    return os.environ["EVENTHUB_NAME"]


@dataclass
class PublishResult:
    call_id: str
    event_type: str
    latency_s: float


class EventHubSink:
    """A sink compatible with `cccp_platform.events.EventSequencer`: call it
    with each `Event` as it's produced. Batches per flush for throughput,
    but under load-test usage we flush per call so publish latency is
    measurable per event.
    """

    def __init__(self) -> None:
        self._producer = EventHubProducerClient.from_connection_string(
            _connection_str(), eventhub_name=_hub_name())
        self.publish_latencies_s: list[float] = []

    def __call__(self, evt) -> None:
        t0 = time.perf_counter()
        batch = self._producer.create_batch(partition_key=evt.call_id)
        batch.add(EventData(json.dumps(evt.to_dict())))
        self._producer.send_batch(batch)
        self.publish_latencies_s.append(time.perf_counter() - t0)

    def close(self) -> None:
        self._producer.close()


def read_recent(max_events: int = 100, timeout_s: float = 5.0) -> list[dict]:
    """Drains up to `max_events` from the hub for inspection/verification
    (starts from the earliest offset still retained -- fine for a demo hub
    with 1-day retention and low volume, not a production consumer pattern).
    """
    out: list[dict] = []
    client = EventHubConsumerClient.from_connection_string(
        _connection_str(), consumer_group="$Default", eventhub_name=_hub_name())

    def on_event(partition_context, event):
        if event is not None:
            out.append(json.loads(event.body_as_str()))
        if len(out) >= max_events:
            raise KeyboardInterrupt  # cheap way to break receive() below

    try:
        with client:
            client.receive(on_event=on_event, starting_position="-1", max_wait_time=timeout_s)
    except KeyboardInterrupt:
        pass
    return out
