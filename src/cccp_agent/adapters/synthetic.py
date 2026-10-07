"""Synthetic adapters implementing the production ports with production-shaped data.

Everything here is SYNTHETIC. Fault injection lets the eval suite exercise
every degradation branch in `agent.py` without a real dependency outage.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from ..domain import Case, Customer, GuidanceChunk, Interaction, OfferEvent, Product, ProductScore
from ..ports import DependencyError, DependencyTimeout

DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "synthetic"


def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _load(name: str, data_dir: Path) -> Any:
    return json.loads((data_dir / name).read_text())


class SyntheticEstate:
    """Loads all synthetic fixtures once; hands out one port instance per call."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        self.catalog = {p["product_id"]: Product(p["product_id"], p["name"], tuple(p["regions"]),
                                                   tuple(p["segments"]), p["min_propensity"], p["priority"])
                        for p in _load("products.json", data_dir)}
        customer_rows = _load("customers.json", data_dir)
        self._customers = {
            c["customer_id"]: Customer(
                c["customer_id"], c["segment"], c["region"], tuple(c["products"]), tuple(c.get("flags", [])),
                tuple(Case(k["case_id"], k["case_type"], k["status"], _date(k["opened_at"]),
                           _date(k.get("resolved_at"))) for k in c.get("cases", [])))
            for c in customer_rows}
        # Display name is presentation-only -- the policy engine never sees it
        # (Customer above has no such field), it's purely so the Workbench can
        # show who's on the call instead of just a customer_id.
        self._display_names = {c["customer_id"]: c.get("name", "") for c in customer_rows}
        self._interactions = [
            Interaction(i["interaction_id"], i["customer_id"], _date(i["occurred_at"]), i["channel"],
                        i["sentiment_score"], tuple(i["themes"]), i["summary"],
                        tuple(OfferEvent(o["product_id"], o["outcome"]) for o in i.get("offers", [])))
            for i in _load("interactions.json", data_dir)]
        self._scores = _load("model_scores.json", data_dir)
        self._guidance = [
            GuidanceChunk(g["document_id"], g["version"], g["section"], _date(g["effective_date"]),
                          tuple(g["product_ids"]), g["situation"], g["text"], tuple(g.get("prohibited_phrases", [])),
                          g.get("source_url", ""))
            for g in _load("guidance.json", data_dir)]

    @property
    def guidance_chunks(self) -> list[GuidanceChunk]:
        """The raw, unfiltered guidance fixtures -- for callers that need to
        browse all approved guidance rather than search it for one product/situation."""
        return list(self._guidance)

    def customer_port(self) -> "SyntheticCustomers":
        return SyntheticCustomers(self._customers)

    def display_name(self, customer_id: str) -> str:
        return self._display_names.get(customer_id, "")

    def interaction_port(self, fault: str | None = None) -> "SyntheticInteractions":
        return SyntheticInteractions(self._interactions, fault)

    def model_port(self, fault: str | None = None) -> "SyntheticPropensityModel":
        return SyntheticPropensityModel(self._scores, fault)

    def guidance_port(self, fault: str | None = None) -> "SyntheticGuidanceIndex":
        return SyntheticGuidanceIndex(self._guidance, fault)


class SyntheticCustomers:
    def __init__(self, customers: dict[str, Customer]) -> None:
        self._c = customers

    def get_customer(self, customer_id: str) -> Customer:
        if customer_id not in self._c:
            raise DependencyError(f"customer {customer_id} not found")
        return self._c[customer_id]


class SyntheticInteractions:
    def __init__(self, rows: list[Interaction], fault: str | None) -> None:
        self._rows, self._fault = rows, fault

    def get_interactions(self, customer_id: str, since: date) -> list[Interaction]:
        if self._fault == "error":
            raise DependencyError("interaction store unavailable")
        return [r for r in self._rows if r.customer_id == customer_id and r.occurred_at >= since]


class SyntheticPropensityModel:
    """Same contract as a real cross-sell microservice: fixed feature schema, versioned output."""
    MODEL_NAME, MODEL_VERSION = "xsell-propensity", "2026.09.1-synthetic"

    def __init__(self, scores: dict[str, dict[str, float]], fault: str | None) -> None:
        self._scores, self._fault = scores, fault
        self.calls = 0

    def score_products(self, customer_id: str) -> list[ProductScore]:
        self.calls += 1
        if self._fault == "timeout":
            raise DependencyTimeout("xsell-propensity timed out after 800ms")
        if self._fault == "error":
            raise DependencyError("xsell-propensity HTTP 503")
        return [ProductScore(pid, p, self.MODEL_NAME, self.MODEL_VERSION)
                for pid, p in self._scores.get(customer_id, {}).items()]


class SyntheticGuidanceIndex:
    """Stands in for a hybrid search index over the approved-guidance source of truth."""

    def __init__(self, chunks: list[GuidanceChunk], fault: str | None) -> None:
        self._chunks, self._fault = chunks, fault

    def search_commercial_guidance(self, product_id: str, situation: str) -> list[GuidanceChunk]:
        if self._fault == "error":
            raise DependencyError("guidance index unavailable")
        hits = [c for c in self._chunks if product_id in c.product_ids and c.situation in (situation, "any")]
        # Exact situation first, then generic.
        return sorted(hits, key=lambda c: (c.situation != situation, c.section))


class StubNarrator:
    """Deterministic narrator stand-in with failure modes for evaluation.

    modes: faithful | wrong_product | invented_number | prohibited_claim | uncited | malformed | timeout
           | identifier_number | spelled_number | obfuscated_product
    """

    def __init__(self, mode: str = "faithful") -> None:
        self.mode = mode
        self.last_payload: dict | None = None
        self.calls = 0

    def generate_json(self, profile: str, system: str, payload: dict, timeout_s: float) -> dict:
        self.calls += 1
        self.last_payload = payload
        if self.mode == "timeout":
            raise DependencyTimeout(f"narrator exceeded {timeout_s}s")
        p, ctx, g = payload["recommended_product"], payload["customer_context"], payload["approved_guidance"][0]
        text = (f"{p['name']} is the top eligible option (propensity {p['propensity_pct']}%). "
                f"Relationship trend is {ctx['sentiment_trend_12m']}; recent themes: {', '.join(ctx['key_themes'])}. "
                f"Per {g['document_id']} section {g['section']}, acknowledge the customer's situation first and "
                f"present the product as optional.")
        out = {"product_id": p["product_id"], "explanation": text, "cited_document_ids": [g["document_id"]]}
        if self.mode == "wrong_product":
            out["product_id"] = "premium_card"
            out["explanation"] = text.replace(p["name"], "Premium Card")
        elif self.mode == "invented_number":
            out["explanation"] = text + " Mention the 5.5% welcome bonus rate."
        elif self.mode == "prohibited_claim":
            out["explanation"] = text + " Tell the customer it is a guaranteed return."
        elif self.mode == "uncited":
            out["cited_document_ids"] = []
        elif self.mode == "malformed":
            out = {"text": text}
        elif self.mode == "identifier_number":  # "12" only exists in the payload as part of `sentiment_trend_12m`
            out["explanation"] = text + " It pays 12% a year."
        elif self.mode == "spelled_number":
            out["explanation"] = text + " Mention the five percent welcome bonus."
        elif self.mode == "obfuscated_product":
            out["explanation"] = text + " Simpler than the Premium-Card."
        return out
