"""Minimal HTTP wrapper so the load test can be triggered and its result
fetched remotely -- the point of running it from here (Container Apps, same
Azure region as the backends) rather than from a laptop is eliminating the
public-internet hop that dominated the first load test's latency numbers.

Not the Workbench: no UI, no scripted-call playback for a human to watch.
One job -- run tools/loadtest.py's logic in-process and return the report.
All config (AZURE_OPENAI_*, EVENTHUB_*, AZURE_SEARCH_*, REDIS_*) comes from
environment variables, provided as Container Apps secrets, never baked into
the image.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from cccp_agent.adapters.synthetic import SyntheticEstate  # noqa: E402
from loadtest import run_level  # noqa: E402

PORT = 8080
# Every load-test call hits paid services (Azure OpenAI, Search, Redis, Event
# Hubs) and the endpoint is public: cap what one request can trigger.
MAX_CONCURRENCY = 10
MAX_CALLS_PER_LEVEL = 20
MAX_LEVELS = 5
_last_report: dict | None = None
_running = False
_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            return self._json(200, {"status": "ok"})
        if path == "/report":
            return self._json(200, _last_report or {"status": "no report yet"})
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/loadtest":
            return self.send_error(404)
        global _running
        # Validate before taking the run slot: a bad query string used to raise
        # after `_running = True`, leaving every later request stuck on 409.
        qs = parse_qs(parsed.query)
        try:
            levels = [int(x) for x in qs.get("concurrency", ["1,3,6"])[0].split(",")]
            calls = int(qs.get("calls", ["6"])[0])
        except ValueError:
            return self._json(400, {"status": "concurrency and calls must be integers"})
        if not (0 < len(levels) <= MAX_LEVELS and all(0 < c <= MAX_CONCURRENCY for c in levels)
                and 0 < calls <= MAX_CALLS_PER_LEVEL):
            return self._json(400, {"status": "out of range", "max_levels": MAX_LEVELS,
                                     "max_concurrency": MAX_CONCURRENCY, "max_calls": MAX_CALLS_PER_LEVEL})
        with _lock:
            if _running:
                return self._json(409, {"status": "already running"})
            _running = True
        threading.Thread(target=self._run, args=(levels, calls), daemon=True).start()
        return self._json(202, {"status": "started", "concurrency": levels, "calls_per_level": calls})

    def _run(self, levels: list[int], calls: int) -> None:
        global _last_report, _running
        try:
            estate = SyntheticEstate()
            report = {"levels": [], "source": "container-apps-same-region"}
            for c in levels:
                report["levels"].append(run_level(c, calls, estate, narrator_timeout_s=20.0))
            _last_report = report
        except Exception as e:  # noqa: BLE001 - surface the failure via /report instead of dying silently
            _last_report = {"status": "error", "error": f"{type(e).__name__}: {e}"}
        finally:
            _running = False

    def log_message(self, fmt: str, *args) -> None:
        pass


def main() -> None:
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"cloud_server listening on :{PORT}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
