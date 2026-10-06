"""The Workbench, pointed at the real Azure backends and running the full
voice pipeline -- not just text. Same frontend (apps/web/) and NDJSON
streaming protocol as apps/api/server.py; the difference is entirely in
what each event is sourced from:

  customer utterance text -> Azure TTS (synthesise) -> Azure STT (transcribe)
  recognised text         -> Azure AI Language (real sentiment)
  recognised text         -> keyword taxonomy match (theme; not a trained classifier)
  every step              -> a real Redis round trip (hot state) and a real
                              Event Hubs publish (domain event)
  trigger fires            -> the real CommercialDecisionAgent: policy gate,
                              propensity model (synthetic), Azure AI Search
                              (guidance), Azure OpenAI (narrator)

Every one of those steps is emitted to the browser as a `pipeline.stage`
event (running, then done with real latency) in addition to the normal
domain events, so the Workbench can render a full, honest trace of what
actually ran -- see apps/web/app.js's STAGE_CATALOG for how each stage is
presented and which ones are real cloud calls vs. local stand-ins.

Run: set AZURE_OPENAI_*, EVENTHUB_*, AZURE_SEARCH_*, REDIS_*,
AZURE_SPEECH_*, AZURE_LANGUAGE_* (see .env.azure, not committed), then
`python apps/api/cloud_workbench_server.py`. Deployed to Azure Container
Apps as a second entry point into the image server.py/cloud_server.py
already ship in (see Dockerfile) -- selected by overriding the container's
start command, not a separate image.
"""
from __future__ import annotations

import base64
import json
import queue
import re
import sys
import threading
import time
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
from cccp_platform.azure_backends.sentiment_language import analyse_sentiment, tag_themes  # noqa: E402
from cccp_platform.azure_backends.speech import synthesize as speech_synthesize  # noqa: E402
from cccp_platform.azure_backends.speech import transcribe as speech_transcribe  # noqa: E402
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

# Decision-time spans (from CommercialDecisionAgent's own tracing) rolled up
# into the same stage vocabulary the browser already knows about. The four
# local/instant ones (get_customer, get_interactions, analyse,
# customer_gates) are all synthetic-data/local-compute -- summed into one
# "gate" row rather than four near-zero-ms rows nobody needs to see
# separately.
_GATE_SPANS = {"get_customer", "get_interactions", "analyse", "customer_gates"}
_SPAN_TO_STAGE = {"score_products": "ml", "search_guidance": "search", "narrator_explain": "narrator"}

estate = SyntheticEstate()
store = AnalyticalStore(DB_DIR / "workbench_cloud.db")
SCRIPTS = {f.stem: json.loads(f.read_text()) for f in sorted(CALLS_DIR.glob("*.json"))}
call_queues: dict[str, "queue.Queue"] = {}


def start_call(script_id: str) -> str:
    script = SCRIPTS[script_id]
    run_id = f"{script['call_id']}_{uuid.uuid4().hex[:6]}"
    q: queue.Queue = queue.Queue()
    call_queues[run_id] = q

    def browser_put(event_type: str, payload: dict) -> None:
        q.put({"event_type": event_type, "call_id": run_id, "payload": payload})

    last_running_instance: list[str | None] = [None]

    def stage_start(stage: str) -> str:
        instance_id = uuid.uuid4().hex[:8]
        last_running_instance[0] = instance_id
        browser_put("pipeline.stage", {"stage": stage, "status": "running", "instance_id": instance_id})
        return instance_id

    def worker() -> None:
        try:
            eh_sink = EventHubSink()

            def dual_sink(evt) -> None:
                eh_sink(evt)
                d = evt.to_dict()
                d["payload"]["_eh_publish_ms"] = round(eh_sink.publish_latencies_s[-1] * 1000, 1)
                q.put(d)

            def stage_done(stage: str, instance_id: str, ms: float, detail: dict | None = None) -> None:
                # A real fact about this call -> goes through the real Event Hubs
                # publish too, not just the browser (same as every other event).
                last_running_instance[0] = None
                payload = {"stage": stage, "status": "done", "instance_id": instance_id, "ms": round(ms, 1)}
                if detail:
                    payload["detail"] = detail
                seq.emit("pipeline.stage", payload)

            trace_id = uuid.uuid4().hex[:16]
            seq = EventSequencer(run_id, script["customer_id"], trace_id, "cloud-workbench-voice", dual_sink)
            state = RedisCallState(run_id, script["customer_id"], script["agent_id"])
            agent = CommercialDecisionAgent(estate.customer_port(), estate.interaction_port(), estate.model_port(),
                                             AzureSearchGuidanceIndex(), estate.catalog, AzureOpenAINarrator(),
                                             narrator_timeout_s=20.0)
            decisions = []

            seq.emit(CALL_STARTED, {"customer_id": script["customer_id"], "agent_id": script["agent_id"]})

            for turn in script["utterances"]:
                if turn["channel"] != "customer":
                    seq.emit(UTTERANCE_FINAL, {"channel": turn["channel"], "text": turn["text"]})
                    continue

                # --- real voice path: synthesise this line, transcribe it back ---
                iid = stage_start("stt")
                t0 = time.perf_counter()
                audio = speech_synthesize(turn["text"])
                tts_s = time.perf_counter() - t0
                t0 = time.perf_counter()
                stt_result = speech_transcribe(audio)
                stt_s = time.perf_counter() - t0
                stage_done("stt", iid, (tts_s + stt_s) * 1000, {"original": turn["text"],
                                                                 "recognized": stt_result.recognized_text,
                                                                 "tts_s": round(tts_s, 2), "stt_s": round(stt_s, 2)})
                # Audio is browser-only, never published to Event Hubs -- domain events
                # carry facts about a call, never raw audio (docs/architecture.md §8.1).
                browser_put("pipeline.audio", {"instance_id": iid,
                                                "audio_base64": base64.b64encode(audio).decode("ascii")})
                recognized = stt_result.recognized_text or turn["text"]
                seq.emit(UTTERANCE_FINAL, {"channel": "customer", "text": turn["text"],
                                            "_stt": {"recognized_text": recognized}})

                # --- real sentiment ---
                iid = stage_start("sentiment")
                t0 = time.perf_counter()
                sentiment = analyse_sentiment(recognized)
                stage_done("sentiment", iid, (time.perf_counter() - t0) * 1000, {"score": sentiment})

                # --- theme tagging (local keyword match, not a cloud call) ---
                iid = stage_start("theme")
                t0 = time.perf_counter()
                themes = tag_themes(recognized)
                stage_done("theme", iid, (time.perf_counter() - t0) * 1000, {"themes": themes})

                # --- hot state (real Redis) ---
                iid = stage_start("redis")
                rolling = state.update_sentiment(sentiment)
                stage_done("redis", iid, state.round_trip_latencies_s[-1] * 1000, {"rolling_sentiment": round(rolling, 3)})
                seq.emit(SENTIMENT_UPDATED, {"utterance_sentiment": sentiment, "rolling_sentiment": round(rolling, 3)})

                if themes:
                    iid = stage_start("redis")
                    active = state.update_themes(themes)
                    stage_done("redis", iid, state.round_trip_latencies_s[-1] * 1000, {"active_themes": active})
                    seq.emit(THEME_DETECTED, {"themes": themes, "active_themes": active})

                if not turn.get("trigger"):
                    continue

                # --- the real decision: policy gate -> ML score -> guidance -> narrator ---
                live = LiveCallSignal(run_id, state.current_sentiment or 0.0, tuple(state.active_themes))
                result = agent.run(DecisionRequest(script["customer_id"], AS_OF, call_id=run_id, live_signal=live))
                decisions.append(result)
                state.set_commercial_state(result.outcome.value)

                gate_ms = sum(s["duration_ms"] for s in result.spans if s["name"] in _GATE_SPANS)
                seq.emit("pipeline.stage", {"stage": "gate", "status": "done", "instance_id": uuid.uuid4().hex[:8],
                                             "ms": round(gate_ms, 1),
                                             "detail": {"policy_decisions": [d.rule_id for d in result.policy_decisions]}})
                for s in result.spans:
                    stage_key = _SPAN_TO_STAGE.get(s["name"])
                    if stage_key:
                        seq.emit("pipeline.stage", {"stage": stage_key, "status": "done",
                                                     "instance_id": uuid.uuid4().hex[:8], "ms": s["duration_ms"]})
                seq.emit("pipeline.stage", {"stage": "evidence", "status": "done", "instance_id": uuid.uuid4().hex[:8],
                                             "ms": 0, "detail": {"evidence_count": len(result.evidence)}})

                seq.emit(DECISION_MADE, {
                    "outcome": result.outcome.value,
                    "policy_decisions": [d.rule_id for d in result.policy_decisions],
                    "degraded": list(result.degraded),
                    "trace_id": result.trace_id,
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
            rec = build_enrichment(_StateAdapter(state, run_id, script, trace_id), decisions)
            store.insert_call(rec)
            q.put({"event_type": "postcall.enrichment_completed", "call_id": run_id,
                   "payload": {"summary": rec.summary, "outcome": rec.outcome, "product_id": rec.product_id}})
            eh_sink.close()
        except Exception as e:  # noqa: BLE001 - a demo must show its own failures, never hang silently
            if last_running_instance[0]:
                browser_put("pipeline.stage", {"stage": "error", "status": "error",
                                                "instance_id": last_running_instance[0],
                                                "detail": {"error": f"{type(e).__name__}: {e}"}})
            browser_put("pipeline.error", {"error": f"{type(e).__name__}: {e}"})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()
    return run_id


class _StateAdapter:
    """Shapes RedisCallState into the attributes build_enrichment() expects
    from the local, in-memory CallState -- avoids changing postcall.py for
    a one-off field-name difference between the two state backends."""

    def __init__(self, state: RedisCallState, call_id: str, script: dict, trace_id: str) -> None:
        snap = state.snapshot()
        self.call_id, self.customer_id, self.agent_id = call_id, script["customer_id"], script["agent_id"]
        self.current_sentiment = state.current_sentiment
        self.sentiment_series = json.loads(snap.get("sentiment_series", "[]"))
        self.active_themes = state.active_themes
        self.trace_id = trace_id


class Handler(BaseHTTPRequestHandler):
    server_version = "CCCPCloudWorkbench/0.2"

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
            return self._json(200, [{"id": k, "title": v.get("title", k), "customer_id": v.get("customer_id")}
                                     for k, v in SCRIPTS.items()])
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
    print(f"CCCP Cloud Workbench (real Azure backends, voice path): :{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
