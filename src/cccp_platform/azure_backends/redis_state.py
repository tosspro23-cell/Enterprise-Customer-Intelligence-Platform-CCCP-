"""Hot call state backed by a real Redis instance (Azure Managed Redis in
production), replacing the in-process dict `cccp_platform.call_state` uses
for the local Workbench demo. Same fields, same TTL-per-call idea
(docs/architecture.md §9.5), but every read/write is now a real network
round trip -- which is exactly what a load test needs to measure.
"""
from __future__ import annotations

import json
import os
import time

import redis

TTL_SECONDS = 24 * 3600
ROLLING_ALPHA = 0.5
THEME_CAP = 5


def _client() -> redis.Redis:
    # The database is provisioned with clustering-policy=EnterpriseCluster
    # (see tools/loadtest.py setup notes): Azure proxies sharding behind one
    # endpoint, so a plain, non-cluster-aware client with standard TLS
    # hostname verification works -- no MOVED redirects, no per-node IP
    # certs to deal with. (The default policy, OSSCluster, pushes the
    # client to connect directly to internal shard IPs, which don't match
    # the cluster's TLS cert -- not worth it for one small hot-state hash
    # per call.)
    host = os.environ["REDIS_HOST"]
    port = int(os.environ.get("REDIS_PORT", "10000"))
    password = os.environ["REDIS_PASSWORD"]
    return redis.Redis(host=host, port=port, password=password, ssl=True, decode_responses=True,
                        socket_timeout=5, socket_connect_timeout=5)


class RedisCallState:
    """One instance per call; each method is one real Redis round trip."""

    def __init__(self, call_id: str, customer_id: str, agent_id: str, client: redis.Redis | None = None) -> None:
        self.call_id, self.customer_id, self.agent_id = call_id, customer_id, agent_id
        self._r = client or _client()
        # Hash tag ({call_id}) forces every key for this call onto the same
        # cluster slot: EnterpriseCluster policy still partitions the
        # keyspace behind its proxy, so a pipeline touching call:*,
        # call:*:series and call:*:themes together still needs them
        # co-located even though the proxy (not the client) handles routing.
        self._key = f"call:{{{call_id}}}"
        self.round_trip_latencies_s: list[float] = []
        self._r.hset(self._key, mapping={"customer_id": customer_id, "agent_id": agent_id, "status": "active"})
        self._r.expire(self._key, TTL_SECONDS)

    def _timed(self, fn, *a, **kw):
        t0 = time.perf_counter()
        result = fn(*a, **kw)
        self.round_trip_latencies_s.append(time.perf_counter() - t0)
        return result

    def update_sentiment(self, score: float) -> float:
        def _do():
            raw = self._r.hget(self._key, "current_sentiment")
            current = ROLLING_ALPHA * score + (1 - ROLLING_ALPHA) * float(raw) if raw is not None else score
            pipe = self._r.pipeline()
            pipe.rpush(f"{self._key}:series", score)
            pipe.hset(self._key, "current_sentiment", current)
            pipe.expire(self._key, TTL_SECONDS)
            pipe.expire(f"{self._key}:series", TTL_SECONDS)
            pipe.execute()
            return current
        return self._timed(_do)

    def update_themes(self, themes: list[str]) -> list[str]:
        def _do():
            key = f"{self._key}:themes"
            pipe = self._r.pipeline()
            for t in themes:
                pipe.lrem(key, 0, t)
                pipe.lpush(key, t)
            pipe.ltrim(key, 0, THEME_CAP - 1)
            pipe.expire(key, TTL_SECONDS)
            pipe.execute()
            return self._r.lrange(key, 0, -1)
        return self._timed(_do)

    @property
    def current_sentiment(self) -> float | None:
        raw = self._r.hget(self._key, "current_sentiment")
        return float(raw) if raw is not None else None

    @property
    def active_themes(self) -> list[str]:
        return self._r.lrange(f"{self._key}:themes", 0, -1)

    def set_commercial_state(self, outcome: str) -> None:
        self._r.hset(self._key, "commercial_state", outcome)

    def end(self) -> None:
        self._r.hset(self._key, "status", "ended")

    def snapshot(self) -> dict:
        h = self._r.hgetall(self._key)
        series = [float(x) for x in self._r.lrange(f"{self._key}:series", 0, -1)]
        h["sentiment_series"] = json.dumps(series)
        return h
