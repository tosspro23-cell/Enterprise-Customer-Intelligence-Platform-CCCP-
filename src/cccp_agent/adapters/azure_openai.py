"""Azure OpenAI adapter for NarratorPort.

Used by `evals/run_evals.py --narrator azure`, the load test and the cloud
Workbench; the default CI eval run uses StubNarrator instead. Requires
`pip install .[azure]` and env vars: AZURE_OPENAI_ENDPOINT,
AZURE_OPENAI_DEPLOYMENT (or an explicit `deployment`), AZURE_OPENAI_API_VERSION,
and either AZURE_OPENAI_API_KEY or Entra ID (azure-identity) credentials.
In production the endpoint should be an AI-gateway URL, not the raw resource.

`timeout_s` is an end-to-end DEADLINE, not a per-request hint: the SDK's
default retries are disabled (a retried timeout used to turn a 2.5s budget
into ~3x that plus backoff) and the call runs in a worker thread whose
result is abandoned once the deadline passes, so the agent always gets
control back on time and falls back to the template.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from ..ports import DependencyError, DependencyTimeout

# Shared, bounded pool: an abandoned call keeps its worker until the HTTP
# timeout (also `timeout_s`) ends it, so this caps how many can pile up.
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="narrator")


class AzureOpenAINarrator:
    def __init__(self, deployment: str | None = None, client: Any | None = None) -> None:
        self.deployment = deployment or os.environ["AZURE_OPENAI_DEPLOYMENT"]
        self.last_call: dict[str, Any] = {}
        if client is not None:  # injected (tests); no SDK import needed
            self._client, self._timeout_exc = client, ()
            return
        try:
            from openai import APITimeoutError, AzureOpenAI  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("pip install .[azure] to use AzureOpenAINarrator") from e
        self._timeout_exc = (APITimeoutError,)
        kwargs: dict[str, Any] = {
            "azure_endpoint": os.environ["AZURE_OPENAI_ENDPOINT"],
            "api_version": os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            "max_retries": 0,  # the deadline belongs to the agent; retries would silently exceed it
        }
        if os.environ.get("AZURE_OPENAI_API_KEY"):
            kwargs["api_key"] = os.environ["AZURE_OPENAI_API_KEY"]
        else:  # Managed Identity / Entra ID
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider  # type: ignore
            kwargs["azure_ad_token_provider"] = get_bearer_token_provider(
                DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
        self._client = AzureOpenAI(**kwargs)

    def _call(self, system: str, payload: dict, timeout_s: float):
        return self._client.chat.completions.create(
            model=self.deployment,
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

    def generate_json(self, profile: str, system: str, payload: dict, timeout_s: float) -> dict:
        t0 = time.perf_counter()
        self.last_call = {"deployment": self.deployment, "deadline_s": timeout_s, "attempts": 1,
                          "deadline_exceeded": False}
        future = _POOL.submit(self._call, system, payload, timeout_s)
        try:
            resp = future.result(timeout=timeout_s)
        except FutureTimeout as e:
            self.last_call["deadline_exceeded"] = True
            raise DependencyTimeout(f"narrator deadline {timeout_s}s exceeded") from e
        except self._timeout_exc as e:
            self.last_call["deadline_exceeded"] = True
            raise DependencyTimeout(str(e)) from e
        except Exception as e:  # noqa: BLE001 - normalise all provider errors
            raise DependencyError(f"{type(e).__name__}: {e}") from e
        finally:
            self.last_call["elapsed_s"] = round(time.perf_counter() - t0, 3)
        try:
            return json.loads(resp.choices[0].message.content or "")
        except json.JSONDecodeError:
            return {"_unparseable": True}
