"""Concurrency smoke test: runs the scripted golden call concurrently against
the real Azure backends (Event Hubs, Azure Managed Redis, Azure OpenAI) at a
few concurrency levels, and reports per-stage latency plus the error rate.
At the default 6 calls per level this is a smoke test, not a load test: it
shows the stack works concurrently, not how it scales.

Guidance retrieval uses the synthetic in-process index (estate.guidance_port()),
not Azure AI Search -- AI Search was removed from the stack entirely
(README.md "Cost tracking"), so guidance_search_ms below reflects an
in-process lookup, not a network call; it's reported for parity with the
live demo's own trace, not as an infrastructure latency measurement.

Statistics: min/median/max always; p95/p99 only when a stage has at least
MIN_N_FOR_TAIL samples -- with fewer, a "p95" is just the maximum.

This measures the managed services' real behaviour under concurrent load,
not the local Workbench server (stdlib http.server was never meant to be
load-tested; see README.md). Requires AZURE_OPENAI_*, EVENTHUB_*, REDIS_* in
the environment (see .env.azure, not committed).

Usage:
  python tools/loadtest.py --concurrency 1,3,6 --calls-per-level 6
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cccp_agent import CommercialDecisionAgent  # noqa: E402
from cccp_agent.adapters.azure_openai import AzureOpenAINarrator  # noqa: E402
from cccp_agent.adapters.synthetic import SyntheticEstate  # noqa: E402
from cccp_platform.azure_backends.cloud_processor import run_call_cloud  # noqa: E402
from cccp_platform.azure_backends.event_hub_bus import EventHubSink  # noqa: E402

AS_OF = date(2026, 10, 5)
SCRIPT_PATH = ROOT / "data" / "calls" / "golden_savings.json"


MIN_N_FOR_TAIL = 30


def _percentiles(xs: list[float]) -> dict[str, float | None]:
    """Seconds in, milliseconds out. Tail percentiles are None below MIN_N_FOR_TAIL samples."""
    if not xs:
        return {"n": 0, "min": None, "p50": None, "max": None, "p95": None, "p99": None}
    xs = sorted(xs)

    def pct(p: float) -> float:
        k = min(len(xs) - 1, int(round(p * (len(xs) - 1))))
        return round(xs[k] * 1000, 1)
    tail = len(xs) >= MIN_N_FOR_TAIL
    return {"n": len(xs), "min": round(xs[0] * 1000, 1), "p50": round(statistics.median(xs) * 1000, 1), "max": round(xs[-1] * 1000, 1),
            "p95": pct(0.95) if tail else None, "p99": pct(0.99) if tail else None}


def _one_call(estate: SyntheticEstate, narrator_timeout_s: float, run_idx: int) -> dict:
    script = json.loads(SCRIPT_PATH.read_text())
    script["call_id"] = f"{script['call_id']}_lt{run_idx:04d}_{int(time.time() * 1000) % 100000}"

    sink = EventHubSink()
    # Guidance retrieval uses the same synthetic in-process index the live
    # demo now uses (cloud_workbench_server.py), not AzureSearchGuidanceIndex
    # -- AI Search was removed from the stack entirely (README.md "Cost
    # tracking"), so this would otherwise fail fast against a deleted
    # service and mask the one thing this script still exists to measure:
    # real narrator/Redis/Event Hubs latency under concurrency.
    agent = CommercialDecisionAgent(
        estate.customer_port(), estate.interaction_port(), estate.model_port(),
        estate.guidance_port(), estate.catalog, AzureOpenAINarrator(), narrator_timeout_s=narrator_timeout_s)

    t0 = time.perf_counter()
    error = None
    decisions = []
    state = None
    try:
        state, decisions = run_call_cloud(script, agent, sink, AS_OF)
    except Exception as e:  # noqa: BLE001 - a load test records failures, it doesn't hide them
        error = f"{type(e).__name__}: {e}"
    finally:
        sink.close()
    total_s = time.perf_counter() - t0

    narrator_ms, guidance_ms = [], []
    for d in decisions:
        for s in d.spans:
            if s["name"] == "narrator_explain":
                narrator_ms.append(s["duration_ms"])
            elif s["name"] == "search_guidance":
                guidance_ms.append(s["duration_ms"])

    return {
        "run_idx": run_idx, "error": error, "total_s": total_s,
        "outcome": decisions[-1].outcome.value if decisions else None,
        "publish_latencies_s": sink.publish_latencies_s,
        "redis_latencies_s": state.round_trip_latencies_s if state else [],
        "narrator_ms": narrator_ms, "guidance_ms": guidance_ms,
    }


def run_level(concurrency: int, calls: int, estate: SyntheticEstate, narrator_timeout_s: float) -> dict:
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_one_call, estate, narrator_timeout_s, i) for i in range(calls)]
        for f in as_completed(futures):
            results.append(f.result())

    errors = [r for r in results if r["error"]]
    publish_all = [x for r in results for x in r["publish_latencies_s"]]
    redis_all = [x for r in results for x in r["redis_latencies_s"]]
    narrator_all = [x / 1000 for r in results for x in r["narrator_ms"]]
    guidance_all = [x / 1000 for r in results for x in r["guidance_ms"]]
    total_all = [r["total_s"] for r in results]

    return {
        "concurrency": concurrency, "calls": calls, "errors": len(errors),
        "error_samples": [e["error"] for e in errors[:3]],
        "outcomes": {o: sum(1 for r in results if r["outcome"] == o) for o in set(r["outcome"] for r in results)},
        "total_call_ms": _percentiles(total_all),
        "event_publish_ms": _percentiles(publish_all),
        "redis_roundtrip_ms": _percentiles(redis_all),
        "narrator_ms": _percentiles(narrator_all),
        # Raw, sorted -- the summary percentiles above don't let a reader ask
        # "what fraction would finish inside a 4s budget instead of 3s", which
        # is exactly the question a deadline tradeoff needs answered exactly,
        # not interpolated from three fixed percentile points.
        "narrator_ms_sorted": sorted(round(x * 1000, 1) for x in narrator_all),
        "guidance_search_ms": _percentiles(guidance_all),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", default="1,3,6", help="comma-separated concurrency levels")
    ap.add_argument("--calls-per-level", type=int, default=6)
    ap.add_argument("--narrator-timeout", type=float, default=20.0,
                     help="generous by default so load-test results reflect real latency, not a timeout cutoff")
    ap.add_argument("--out", default=str(ROOT / "var" / "loadtest_report.json"),
                    help="untracked by default; copy into evals/report/ deliberately to publish a run")
    a = ap.parse_args()

    estate = SyntheticEstate()
    levels = [int(x) for x in a.concurrency.split(",")]
    report = {"script": SCRIPT_PATH.name, "narrator_timeout_s": a.narrator_timeout, "levels": []}

    for c in levels:
        print(f"--- concurrency={c}, calls={a.calls_per_level} ---")
        level_result = run_level(c, a.calls_per_level, estate, a.narrator_timeout)
        report["levels"].append(level_result)
        print(json.dumps(level_result, indent=2))

    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
