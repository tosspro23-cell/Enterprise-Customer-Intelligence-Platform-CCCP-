"""Ports (interfaces) to the systems this agent reads from.

Production mapping:
  CustomerDirectoryPort   -> customer data warehouse (read-only, row-access scoped)
  InteractionHistoryPort  -> post-call enrichment store
  PropensityModelPort     -> the existing cross-sell / up-sell scoring microservice
  GuidanceIndexPort       -> an approved-content search index derived from the guidance source of truth
  NarratorPort            -> an LLM reachable behind a gateway

Adapters implement these protocols; the agent never imports a concrete adapter.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Protocol

from .domain import Customer, GuidanceChunk, Interaction, ProductScore


class DependencyError(RuntimeError):
    """A dependency failed. The agent must degrade, never fabricate a substitute."""


class DependencyTimeout(DependencyError):
    pass


class CustomerDirectoryPort(Protocol):
    def get_customer(self, customer_id: str) -> Customer: ...


class InteractionHistoryPort(Protocol):
    def get_interactions(self, customer_id: str, since: date) -> list[Interaction]: ...


class PropensityModelPort(Protocol):
    def score_products(self, customer_id: str) -> list[ProductScore]:
        """Calls the approved model with ITS OWN fixed feature schema.

        Deliberately takes no free-form context features: live-call signals are
        applied in the policy layer, never injected into an approved model's inputs.
        """
        ...


class GuidanceIndexPort(Protocol):
    def search_commercial_guidance(self, product_id: str, situation: str) -> list[GuidanceChunk]: ...


class NarratorPort(Protocol):
    def generate_json(self, profile: str, system: str, payload: dict[str, Any], timeout_s: float) -> dict[str, Any]: ...
