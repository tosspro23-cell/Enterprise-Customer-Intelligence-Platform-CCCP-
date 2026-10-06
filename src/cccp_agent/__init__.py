"""CCCP Commercial Decision Agent.

ML predicts suitability, policy authorises timing and permission, retrieval
grounds the explanation in approved guidance, and the narrator (LLM) only
explains -- it never decides. See `agent.CommercialDecisionAgent.run`.
"""
from .agent import CommercialDecisionAgent
from .domain import DecisionRequest, DecisionResult, LiveCallSignal, Outcome

__all__ = [
    "CommercialDecisionAgent",
    "DecisionRequest",
    "DecisionResult",
    "LiveCallSignal",
    "Outcome",
]
