"""Evaluation runner for the Commercial Decision Agent.

Tests (tests/) check software contracts in isolation. Evals check *behaviour*
across scenarios, including fault-injected dependencies and adversarial
narrator outputs -- the thing that actually matters for a decision system.

Usage:
  python evals/run_evals.py                  # deterministic StubNarrator (CI gate)
  python evals/run_evals.py --narrator azure  # real Azure OpenAI; fault-injection cases are skipped
  python evals/run_evals.py --out evals/report
Exit code 1 if any critical case fails (promotion gate).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cccp_agent import CommercialDecisionAgent, DecisionRequest, LiveCallSignal  # noqa: E402
from cccp_agent.adapters.synthetic import StubNarrator, SyntheticEstate  # noqa: E402
from cccp_agent.narrative import GLOBAL_PROHIBITED  # noqa: E402

GATE_OUTCOMES = {"suppressed", "deferred", "specialist_handoff"}


def run_case(case: dict, estate: SyntheticEstate, as_of: date, narrator_mode: str) -> dict:
    faults = case.get("faults", {})
    model = estate.model_port(faults.get("model"))
    if narrator_mode == "azure":
        from cccp_agent.adapters.azure_openai import AzureOpenAINarrator
        narrator = AzureOpenAINarrator()
    else:
        narrator = StubNarrator(faults.get("narrator", "faithful"))
    agent = CommercialDecisionAgent(
        estate.customer_port(), estate.interaction_port(faults.get("interactions")), model,
        estate.guidance_port(faults.get("guidance")), estate.catalog, narrator)
    inp = case["input"]
    live = LiveCallSignal(**{**inp["live_signal"], "active_themes": tuple(inp["live_signal"].get("active_themes", []))}) \
        if inp.get("live_signal") else None
    res = agent.run(DecisionRequest(inp["customer_id"], as_of, live_signal=live))
    r = res.to_dict()
    exp = dict(case["expect"])
    if narrator_mode == "azure":
        exp.pop("generated_by", None)  # real model: measured, not asserted

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    rec_pid = r["recommendation"]["product_id"] if r["recommendation"] else None
    gate = next((d for d in r["policy_decisions"] if d["subject"] == "customer"), None)
    if "outcome" in exp:
        check("outcome", r["outcome"] == exp["outcome"], f"got {r['outcome']}")
    if "product_id" in exp:
        check("product_id", rec_pid == exp["product_id"], f"got {rec_pid}")
    if "gate_rule" in exp:
        check("gate_rule", gate is not None and any(d["rule_id"] == exp["gate_rule"] for d in r["policy_decisions"]),
              f"decisions {[d['rule_id'] for d in r['policy_decisions']]}")
    if "generated_by" in exp:
        check("generated_by", r["explanation"]["generated_by"] == exp["generated_by"], r["explanation"]["generated_by"])
    if "sentiment_label" in exp:
        check("sentiment_label", r["sentiment_trend"]["label"] == exp["sentiment_label"], r["sentiment_trend"]["label"])
    themes = {t["theme"]: t for t in r["themes"]}
    for t in exp.get("themes_include", []):
        check(f"theme:{t}", t in themes)
    for t in exp.get("recurring_include", []):
        check(f"recurring:{t}", themes.get(t, {}).get("recurring") is True)
    for t in exp.get("emerging_include", []):
        check(f"emerging:{t}", themes.get(t, {}).get("emerging") is True)
    for c in exp.get("citations_include", []):
        check(f"citation:{c}", c in r["explanation"]["cited_document_ids"])
    if "guidance_section" in exp:
        secs = [e["detail"] for e in r["evidence"] if e["kind"] == "guidance_chunk"]
        check("guidance_section", bool(secs) and secs[0].startswith(f"section {exp['guidance_section']}"), str(secs[:1]))
    for pid, rule in exp.get("excluded", {}).items():
        check(f"excluded:{pid}", any(d["subject"] == pid and d["outcome"] == "exclude" and d["rule_id"] == rule
                                     for d in r["policy_decisions"]))
    for d in exp.get("degraded_include", []):
        check(f"degraded:{d}", d in r["degraded"], str(r["degraded"]))
    for e in exp.get("validation_errors_include", []):
        check(f"validation:{e}", any(v.startswith(e) for v in r["explanation"]["validation_errors"]),
              str(r["explanation"]["validation_errors"]))
    if exp.get("model_not_called"):
        check("model_not_called", model.calls == 0, f"calls={model.calls}")
    if exp.get("narrator_not_called") and isinstance(narrator, StubNarrator):
        check("narrator_not_called", narrator.calls == 0, f"calls={narrator.calls}")
    if exp.get("narrator_payload_excludes"):
        blob = json.dumps(getattr(narrator, "last_payload", None) or {}).lower()
        for s in exp["narrator_payload_excludes"]:
            check(f"payload_excludes:{s}", s.lower() not in blob)
    for k in exp.get("evidence_kinds", []):
        check(f"evidence:{k}", any(e["kind"] == k for e in r["evidence"]))

    text = r["explanation"]["text"].lower()
    other = [p.name.lower() for pid, p in estate.catalog.items() if pid != rec_pid]
    policy_violation = r["outcome"] == "recommended" and (
        any(p in text for p in GLOBAL_PROHIBITED) or any(o in text for o in other))
    return {
        "id": case["id"], "title": case["title"], "category": case["category"], "critical": case.get("critical", False),
        "passed": all(ok for _, ok, _ in checks), "checks": checks, "outcome": r["outcome"], "product": rec_pid,
        "generated_by": r["explanation"]["generated_by"], "degraded": r["degraded"],
        "expected_outcome": exp.get("outcome"), "policy_violation_in_final_text": policy_violation,
        "cited": bool(r["explanation"]["cited_document_ids"]),
        "latency_ms": round(sum(s["duration_ms"] for s in r["spans"]), 2), "trace_id": r["trace_id"],
        "explanation": r["explanation"]["text"],
    }


def metrics(results: list[dict]) -> dict:
    with_outcome = [r for r in results if r["expected_outcome"]]
    gate_cases = [r for r in results if r["expected_outcome"] in GATE_OUTCOMES]
    guard = [r for r in results if r["category"] == "narrator_guardrail"]
    recs = [r for r in results if r["outcome"] == "recommended"]
    lat = sorted(r["latency_ms"] for r in results)
    return {
        "cases": len(results),
        "passed": sum(r["passed"] for r in results),
        "critical_failures": [r["id"] for r in results if r["critical"] and not r["passed"]],
        "decision_accuracy": round(sum(r["outcome"] == r["expected_outcome"] for r in with_outcome) / len(with_outcome), 3)
        if with_outcome else None,
        "hard_gate_violations": sum(r["outcome"] == "recommended" for r in gate_cases),
        "narrator_guardrail_catch_rate": round(sum(r["generated_by"] == "template" for r in guard) / len(guard), 3)
        if guard else None,
        "citation_coverage_on_recommendations": round(sum(r["cited"] for r in recs) / len(recs), 3) if recs else None,
        "policy_violations_in_final_text": sum(r["policy_violation_in_final_text"] for r in results),
        "agent_latency_ms_p50": statistics.median(lat) if lat else None,
        "agent_latency_ms_max": max(lat) if lat else None,
    }


def to_markdown(suite: dict, results: list[dict], m: dict, narrator_mode: str) -> str:
    lines = [f"# Eval report - {suite['suite']} v{suite['version']}", "",
             f"Narrator mode: `{narrator_mode}` | as_of: {suite['as_of']} | synthetic data only", "", "## Metrics", "",
             "| metric | value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in m.items()]
    lines += ["", "## Cases", "", "| id | crit | category | result | outcome | product | explanation by | failed checks |",
              "|---|---|---|---|---|---|---|---|"]
    for r in results:
        failed = "; ".join(f"{n} ({d})" for n, ok, d in r["checks"] if not ok) or "-"
        lines.append(f"| {r['id']} | {'Y' if r['critical'] else ''} | {r['category']} | {'PASS' if r['passed'] else 'FAIL'} "
                     f"| {r['outcome']} | {r['product'] or '-'} | {r['generated_by']} | {failed} |")
    lines += ["", "## Sample explanations", ""]
    for r in results:
        if r["id"] in ("EV-01", "EV-02", "EV-15"):
            lines.append(f"- **{r['id']}** ({r['generated_by']}): {r['explanation']}")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--narrator", choices=["stub", "azure"], default="stub")
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases.json"))
    ap.add_argument("--out", default=str(ROOT / "evals" / "report"))
    a = ap.parse_args()
    suite = json.loads(Path(a.cases).read_text())
    estate, as_of = SyntheticEstate(), date.fromisoformat(suite["as_of"])
    cases = suite["cases"]
    if a.narrator == "azure":
        cases = [c for c in cases if c.get("faults", {}).get("narrator", "faithful") == "faithful"]
    results = []
    for c in cases:
        try:
            results.append(run_case(c, estate, as_of, a.narrator))
        except Exception as e:  # an agent crash is a failed case, never a silent skip
            results.append({"id": c["id"], "title": c["title"], "category": c["category"],
                            "critical": c.get("critical", False), "passed": False,
                            "checks": [("no_crash", False, f"{type(e).__name__}: {e}")], "outcome": "crash",
                            "product": None, "generated_by": "-", "degraded": [], "expected_outcome": c["expect"].get("outcome"),
                            "policy_violation_in_final_text": False, "cited": False, "latency_ms": 0.0,
                            "trace_id": "-", "explanation": ""})
    m = metrics(results)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"eval_report_{a.narrator}.md").write_text(to_markdown(suite, results, m, a.narrator))
    (out / f"eval_results_{a.narrator}.json").write_text(json.dumps({"metrics": m, "results": results}, indent=2, default=str))
    for r in results:
        print(f"{'PASS' if r['passed'] else 'FAIL'}  {r['id']}  {r['title']}")
        for n, ok, d in r["checks"]:
            if not ok:
                print(f"        x {n}: {d}")
    print(json.dumps(m, indent=2))
    return 1 if m["critical_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
