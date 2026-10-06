"""The reference-slice backend: stdlib HTTP only, by design (see pyproject.toml
-- the core stays dependency-free). Serves the Workbench page, runs scripted
calls in a background thread streaming newline-delimited JSON events to the
browser, persists post-call facts, and answers the supervisor assistant.

Run: python apps/api/server.py  then open http://127.0.0.1:8765/
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

from cccp_agent import CommercialDecisionAgent  # noqa: E402
from cccp_agent.adapters.synthetic import StubNarrator, SyntheticEstate  # noqa: E402
from cccp_platform import assistant as assistant_mod  # noqa: E402
from cccp_platform.postcall import build_enrichment  # noqa: E402
from cccp_platform.processor import run_call  # noqa: E402
from cccp_platform.store import AnalyticalStore  # noqa: E402

WEB_DIR = ROOT / "apps" / "web"
CALLS_DIR = ROOT / "data" / "calls"
DB_DIR = ROOT / "var"
DB_DIR.mkdir(exist_ok=True)
AS_OF = date(2026, 10, 5)
PORT = 8765

estate = SyntheticEstate()
store = AnalyticalStore(DB_DIR / "workbench.db")
SCRIPTS = {f.stem: json.loads(f.read_text()) for f in sorted(CALLS_DIR.glob("*.json"))}
call_queues: dict[str, "queue.Queue"] = {}


def _make_agent() -> CommercialDecisionAgent:
    e = estate
    return CommercialDecisionAgent(e.customer_port(), e.interaction_port(), e.model_port(),
                                    e.guidance_port(), e.catalog, StubNarrator())


def start_call(script_id: str) -> str:
    script = SCRIPTS[script_id]
    run_id = f"{script['call_id']}_{uuid.uuid4().hex[:6]}"
    q: queue.Queue = queue.Queue()
    call_queues[run_id] = q

    def worker() -> None:
        def sink(evt) -> None:
            q.put(evt.to_dict())

        try:
            state, decisions = run_call(dict(script, call_id=run_id), _make_agent(), sink, AS_OF, pace=True)
            rec = build_enrichment(state, decisions)
            store.insert_call(rec)
            q.put({"event_type": "postcall.enrichment_completed", "call_id": run_id,
                   "payload": {"summary": rec.summary, "outcome": rec.outcome, "product_id": rec.product_id}})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()
    return run_id


class Handler(BaseHTTPRequestHandler):
    server_version = "CCCPWorkbench/0.1"

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
        if path == "/api/scripts":
            return self._json(200, [{"id": k, "title": v.get("title", k)} for k, v in SCRIPTS.items()])
        if path == "/api/mode":
            return self._json(200, {"mode": "local"})
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

    def log_message(self, fmt: str, *args) -> None:  # keep the demo console quiet
        pass


def main() -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"CCCP Workbench reference slice: http://127.0.0.1:{PORT}/")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
