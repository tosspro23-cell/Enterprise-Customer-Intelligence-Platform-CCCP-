"""The Workbench, pointed at the real Azure backends instead of local
stand-ins. Same frontend (apps/web/) and same NDJSON streaming protocol as
apps/api/server.py, so a browser can't tell which one it's talking to
except for the "LIVE AZURE" badge (GET /api/mode) -- the difference is
entirely in what each event is sourced from: real Event Hubs publishes,
real Redis round trips, a real AI Search query, a real Azure OpenAI call.

Run: set AZURE_OPENAI_*, EVENTHUB_*, AZURE_SEARCH_*, REDIS_* (see
.env.azure, not committed), then `python apps/api/cloud_workbench_server.py`.
Deployed to Azure Container Apps as a second entry point into the same
image server.py/cloud_server.py already ship in (see Dockerfile) -- this
file is selected by overriding the container's start command, not by a
separate image.
"""
from __future__ import annotations

import json
import queue
import re
import sys
import threading
import uuid
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from cccp_agent import CommercialDecisionAgent, DecisionRequest, LiveCallSignal  # noqa: E402
from cccp_agent.adapters.azure_openai import AzureOpenAINarrator  # noqa: E402
from cccp_agent.adapters.synthetic import SyntheticEstate  # noqa: E402
from cccp_platform import assistant as assistant_mod  # noqa: E402
from cccp_platform.azure_backends.event_hub_bus import EventHubSink  # noqa: E402
from cccp_platform.azure_backends.redis_state import RedisCallState  # noqa: E402
from cccp_platform.azure_backends.search_guidance import AzureSearchGuidanceIndex  # noqa: E402
from cccp_platform.events import (CALL_ENDED, CALL_STARTED, DECISION_MADE, EventSequencer, SENTIMENT_UPDATED,
                                   SUGGESTION_GENERATED, THEME_DETECTED, UTTERANCE_FINAL)
from cccp_platform.postcall import build_enrichment
from cccp_platform.store import AnalyticalStore

WEB_DIR = ROOT / "apps" / "web"
CALLS_DIR = ROOT / "data" / "calls"
DB_DIR = ROOT / "var"
DB_DIR.mkdir(exist_ok=True)
AS_OF = date(2026, 10, 5)
PORT = 8080

estate = SyntheticEstate()
store = AnalyticalStore(DB_DIR / "workbench_cloud.db")
SCRIPTS = {f.stem: json.loads(f.read_text()) for f in sorted(CALLS_DIR.glob("*.json"))}
call_queues: dict[str, "queue.Queue"] = {}


def start_call(script_id: str) -> str:
    """Runs one scripted call against the real backends, forwarding each
    event to the browser's queue annotated with the real latency it took
    (not just the production event payload) -- the point of this server.
    """
    script = SCRIPTS[script_id]
    run_id = f"{script['call_id']}_{uuid.uuid4().hex[:6]}"
    q: queue.Queue = queue.Queue()
    call_queues[run_id] = q

    def worker() -> None:
        try:
            eh_sink = EventHubSink()

            def dual_sink(evt) -> None:
                eh_sink(evt)  # real publish to Event Hubs -- measures its own latency internally
                d = evt.to_dict()
                d["payload"]["_eh_publish_ms"] = round(eh_sink.publish_latencies_s[-1] * 1000, 1)
                q.put(d)

            trace_id = uuid.uuid4().hex[:16]
            seq = EventSequencer(run_id, script["customer_id"], trace_id, "cloud-workbench", dual_sink)
            state = RedisCallState(run_id, script["customer_id"], script["agent_id"])
            agent = CommercialDecisionAgent(estate.customer_port(), estate.interaction_port(), estate.model_port(),
                                             AzureSearchGuidanceIndex(), estate.catalog, AzureOpenAINarrator(),
                                             narrator_timeout_s=20.0)
            decisions = []

            seq.emit(CALL_STARTED, {"customer_id": script["customer_id"], "agent_id": script["agent_id"]})
            for turn in script["utterances"]:
                seq.emit(UTTERANCE_FINAL, {"channel": turn["channel"], "text": turn["text"]})
                if turn["channel"] != "customer":
                    continue
                sentiment, themes = turn.get("sentiment"), turn.get("themes", [])
                if sentiment is not None:
                    rolling = state.update_sentiment(sentiment)
                    seq.emit(SENTIMENT_UPDATED, {"utterance_sentiment": sentiment, "rolling_sentiment": round(rolling, 3),
                                                  "_redis_ms": round(state.round_trip_latencies_s[-1] * 1000, 1)})
                if themes:
                    active = state.update_themes(themes)
                    seq.emit(THEME_DETECTED, {"themes": themes, "active_themes": active,
                                               "_redis_ms": round(state.round_trip_latencies_s[-1] * 1000, 1)})
                if not turn.get("trigger"):
                    continue

                live = LiveCallSignal(run_id, state.current_sentiment or 0.0, tuple(state.active_themes))
                result = agent.run(DecisionRequest(script["customer_id"], AS_OF, call_id=run_id, live_signal=live))
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
                        "product_id": result.recommendation.product_id, "propensity": result.recommendation.propensity,
                        "explanation": result.explanation.text,
                        "cited_document_ids": list(result.explanation.cited_document_ids),
                        "generated_by": result.explanation.generated_by,
                        "evidence": [e.__dict__ for e in result.evidence],
                    })
            state.end()
            seq.emit(CALL_ENDED, {})
            rec = build_enrichment(_StateAdapter(state, run_id, script), decisions)
            store.insert_call(rec)
            q.put({"event_type": "postcall.enrichment_completed", "call_id": run_id,
                   "payload": {"summary": rec.summary, "outcome": rec.outcome, "product_id": rec.product_id}})
            eh_sink.close()
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()
    return run_id


class _StateAdapter:
    """Shapes RedisCallState into the attributes build_enrichment() expects
    from the local, in-memory CallState -- avoids changing postcall.py for
    a one-off field-name difference between the two state backends."""

    def __init__(self, state: RedisCallState, call_id: str, script: dict) -> None:
        snap = state.snapshot()
        self.call_id, self.customer_id, self.agent_id = call_id, script["customer_id"], script["agent_id"]
        self.current_sentiment = state.current_sentiment
        self.sentiment_series = json.loads(snap.get("sentiment_series", "[]"))
        self.active_themes = state.active_themes


class Handler(BaseHTTPRequestHandler):
    server_version = "CCCPCloudWorkbench/0.1"

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, call_id: str) -> None:
        q = call_queues.get(call_id)
        if q is None:
            return self._json(404, {"error": "unknown call_id"})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-cache")
        self.close_connection = True
        self.end_headers()
        try:
            while True:
                evt = q.get()
                if evt is None:
                    break
                self.wfile.write((json.dumps(evt) + "\n").encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            call_queues.pop(call_id, None)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/":
            return self._file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        if path == "/app.js":
            return self._file(WEB_DIR / "app.js", "application/javascript; charset=utf-8")
        if path == "/style.css":
            return self._file(WEB_DIR / "style.css", "text/css; charset=utf-8")
        if path == "/healthz":
            return self._json(200, {"status": "ok"})
        if path == "/api/mode":
            return self._json(200, {"mode": "azure-live"})
        if path == "/api/scripts":
            return self._json(200, [{"id": k, "title": v.get("title", k)} for k, v in SCRIPTS.items()])
        m = re.fullmatch(r"/api/calls/([^/]+)/stream", path)
        if m:
            return self._stream(m.group(1))
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""

        m = re.fullmatch(r"/api/run/([^/]+)", path)
        if m:
            script_id = m.group(1)
            if script_id not in SCRIPTS:
                return self._json(404, {"error": "unknown script"})
            return self._json(202, {"call_id": start_call(script_id)})

        if path == "/api/assistant/ask":
            try:
                data = json.loads(body or b"{}")
            except json.JSONDecodeError:
                data = {}
            ans = assistant_mod.ask(data.get("question", ""), store, estate.guidance_chunks, estate.catalog)
            return self._json(200, {"answer": ans.answer, "tool": ans.tool, "source": ans.source,
                                     "citations": list(ans.citations), "note": assistant_mod.ROUTER_NOTE})
        self.send_error(404)

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"CCCP Cloud Workbench (real Azure backends): :{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
