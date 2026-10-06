"""The analytical record. Production is the existing data warehouse
(docs/architecture.md §10); this is a SQLite file using the same fact-table
names, so a query written against this store reads the same as one written
against the production semantic views.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .postcall import EnrichmentRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS call_enrichment_fact (
    call_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    final_sentiment REAL,
    sentiment_trajectory TEXT NOT NULL,
    themes TEXT NOT NULL,
    outcome TEXT NOT NULL,
    product_id TEXT,
    generated_by TEXT,
    degraded TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    summary TEXT NOT NULL
);
"""


class AnalyticalStore:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, check_same_thread=False)

    def insert_call(self, rec: EnrichmentRecord) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO call_enrichment_fact "
                "(call_id, customer_id, agent_id, ended_at, final_sentiment, sentiment_trajectory, "
                " themes, outcome, product_id, generated_by, degraded, trace_id, summary) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rec.call_id, rec.customer_id, rec.agent_id, rec.ended_at, rec.final_sentiment,
                 json.dumps(rec.sentiment_trajectory), json.dumps(rec.themes), rec.outcome, rec.product_id,
                 rec.generated_by, json.dumps(rec.degraded), rec.trace_id, rec.summary),
            )

    def outcome_counts(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT outcome, COUNT(*) FROM call_enrichment_fact GROUP BY outcome").fetchall()
        return {outcome: n for outcome, n in rows}

    def avg_final_sentiment(self) -> float | None:
        with self._conn() as c:
            row = c.execute("SELECT AVG(final_sentiment) FROM call_enrichment_fact "
                             "WHERE final_sentiment IS NOT NULL").fetchone()
        return round(row[0], 3) if row and row[0] is not None else None

    def recent_calls(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            c.row_factory = sqlite3.Row
            rows = c.execute("SELECT * FROM call_enrichment_fact ORDER BY ended_at DESC LIMIT ?",
                              (limit,)).fetchall()
        return [dict(r) for r in rows]

    def suggestion_acceptance_rate(self) -> float | None:
        """Placeholder metric: proportion of recommended calls (no feedback capture in this slice)."""
        counts = self.outcome_counts()
        total = sum(counts.values())
        return round(counts.get("recommended", 0) / total, 3) if total else None
