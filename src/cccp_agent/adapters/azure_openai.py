"""Azure OpenAI adapter for NarratorPort.

NOT exercised by the bundled evals (they use StubNarrator). Requires
`pip install .[azure]` and env vars: AZURE_OPENAI_ENDPOINT,
AZURE_OPENAI_DEPLOYMENT, AZURE_OPENAI_API_VERSION, and either
AZURE_OPENAI_API_KEY or Entra ID (azure-identity) credentials.
In production the endpoint should be an AI-gateway URL, not the raw resource.
"""
from __future__ import annotations

import json
import os
from typing import Any

from ..ports import DependencyError, DependencyTimeout


class AzureOpenAINarrator:
    def __init__(self) -> None:
        try:
            from openai import APITimeoutError, AzureOpenAI  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pip install .[azure] to use AzureOpenAINarrator") from e
        self._timeout_exc = APITimeoutError
        kwargs: dict[str, Any] = {
            "azure_endpoint": os.environ["AZURE_OPENAI_ENDPOINT"],
            "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        }
        if os.environ.get("AZURE_OPENAI_API_KEY"):
            kwargs["api_key"] = os.environ["AZURE_OPENAI_API_KEY"]
        else:  # Managed Identity / Entra ID
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # type: ignore
            kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        self._client = AzureOpenAI(**kwargs)
        self._deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    def generate_json(self, profile: str, system: str, payload: dict, timeout_s: float) -> dict:
        try:
            resp = self._client.chat.completions.create(
                model=self._deployment,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": json.dumps(payload)}],
                response_format={"type": "json_object"},
                # Reasoning-family deployments (e.g. gpt-5-mini) reject a custom
                # temperature and the legacy max_tokens param; max_completion_tokens
                # is the unified field both older and newer chat models accept.
                # It must also cover hidden reasoning tokens for those models -- too
                # tight a budget burns entirely on reasoning and returns empty content
                # rather than an error (measured: ~550-650 reasoning tokens for this
                # prompt). 1500 leaves headroom above that plus the real response.
                max_completion_tokens=1500,
                timeout=timeout_s,
            )
        except self._timeout_exc as e:
            raise DependencyTimeout(str(e)) from e
        except Exception as e:  # noqa: BLE001 - normalise all provider errors
            raise DependencyError(f"{type(e).__name__}: {e}") from e
        try:
            return json.loads(resp.choices[0].message.content or "")
        except json.JSONDecodeError:
            return {"_unparseable": True}
