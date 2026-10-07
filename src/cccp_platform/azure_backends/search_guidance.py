"""GuidanceIndexPort implemented against a real Azure AI Search index.

Swaps in for `cccp_agent.adapters.synthetic.SyntheticGuidanceIndex` with the
exact same interface (`search_commercial_guidance`), so
`CommercialDecisionAgent` runs unmodified against a real managed search
service instead of an in-process list scan. `ingest()` is a one-time setup
step: create the index schema, then upload the synthetic guidance fixtures
as documents. Ingest needs an ADMIN key (run it from an operator shell); the
deployed apps are given a read-only QUERY key and only search.

What this validates: connectivity and latency of a managed search service
on the hot path. It is used as a filtered document store (`search_text="*"`,
exact product/situation filter, no ranking), so it says nothing about
retrieval quality; effective dates are enforced by the agent itself.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date
from pathlib import Path

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (SearchableField, SearchField, SearchFieldDataType, SearchIndex,
                                                     SimpleField)

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from cccp_agent.domain import GuidanceChunk  # noqa: E402
from cccp_agent.ports import DependencyError  # noqa: E402


def _endpoint() -> str:
    return os.environ["AZURE_SEARCH_ENDPOINT"]


def _index_name() -> str:
    return os.environ.get("AZURE_SEARCH_INDEX", "guidance")


def _odata_literal(value: str) -> str:
    """Quote a string for an OData filter (single quotes doubled), so a value
    can never close the literal and extend the filter expression."""
    return "'" + value.replace("'", "''") + "'"


def _credential() -> AzureKeyCredential:
    return AzureKeyCredential(os.environ["AZURE_SEARCH_API_KEY"])


def ingest(data_dir: Path = ROOT / "data" / "synthetic") -> int:
    """Creates (or replaces) the guidance index and uploads the synthetic fixtures.

    Returns the number of documents uploaded. Safe to re-run: the index is
    recreated from scratch each time so ingestion is idempotent.
    """
    index_client = SearchIndexClient(_endpoint(), _credential())
    fields = [
        SimpleField(name="id", type=SearchFieldDataType.String, key=True),
        SimpleField(name="document_id", type=SearchFieldDataType.String, filterable=True),
        SimpleField(name="version", type=SearchFieldDataType.String),
        SimpleField(name="section", type=SearchFieldDataType.String),
        SimpleField(name="effective_date", type=SearchFieldDataType.String),
        SearchField(name="product_ids", type=SearchFieldDataType.Collection(SearchFieldDataType.String),
                    filterable=True),
        SimpleField(name="situation", type=SearchFieldDataType.String, filterable=True),
        SearchableField(name="text", type=SearchFieldDataType.String),
        SearchField(name="prohibited_phrases", type=SearchFieldDataType.Collection(SearchFieldDataType.String)),
        SimpleField(name="source_url", type=SearchFieldDataType.String),
    ]
    index_client.create_or_update_index(SearchIndex(name=_index_name(), fields=fields))

    chunks = json.loads((data_dir / "guidance.json").read_text())
    docs = [{
        "id": f"{c['document_id']}-{c['section']}".replace(" ", "_").replace(".", "_"),
        "document_id": c["document_id"], "version": c["version"], "section": c["section"],
        "effective_date": c["effective_date"], "product_ids": c["product_ids"], "situation": c["situation"],
        "text": c["text"], "prohibited_phrases": c.get("prohibited_phrases", []),
        "source_url": c.get("source_url", ""),
    } for c in chunks]

    search_client = SearchClient(_endpoint(), _index_name(), _credential())
    search_client.upload_documents(docs)
    return len(docs)


class AzureSearchGuidanceIndex:
    """GuidanceIndexPort backed by a live Azure AI Search index."""

    def __init__(self) -> None:
        self._client = SearchClient(_endpoint(), _index_name(), _credential())

    def search_commercial_guidance(self, product_id: str, situation: str) -> list[GuidanceChunk]:
        try:
            results = self._client.search(
                search_text="*",
                filter=f"product_ids/any(p: p eq {_odata_literal(product_id)}) and "
                       f"(situation eq {_odata_literal(situation)} or situation eq 'any')",
                select=["document_id", "version", "section", "effective_date", "product_ids", "situation",
                        "text", "prohibited_phrases", "source_url"],
            )
            hits = list(results)
        except Exception as e:  # noqa: BLE001 - normalise provider errors like the other ports do
            raise DependencyError(f"{type(e).__name__}: {e}") from e

        chunks = [GuidanceChunk(
            h["document_id"], h["version"], h["section"], date.fromisoformat(h["effective_date"]),
            tuple(h["product_ids"]), h["situation"], h["text"], tuple(h.get("prohibited_phrases") or []),
            h.get("source_url", ""),
        ) for h in hits]
        return sorted(chunks, key=lambda c: (c.situation != situation, c.section))


if __name__ == "__main__":
    n = ingest()
    print(f"ingested {n} guidance chunks into index '{_index_name()}'")
