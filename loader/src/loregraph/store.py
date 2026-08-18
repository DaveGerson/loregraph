from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TABLE_FILES = {
    "sources": "sources.jsonl",
    "blocks": "blocks.jsonl",
    "chunks": "chunks.jsonl",
    "entities": "entities.jsonl",
    "mentions": "mentions.jsonl",
    "evidence": "evidence.jsonl",
    "assertions": "assertions.jsonl",
    "review_queue": "review_queue.jsonl",
    "embeddings": "embeddings.jsonl",
}


class RecordStore:
    """Small, inspectable canonical store. JSONL is the interchange contract.

    Each write is an idempotent replace-by-id followed by an atomic file swap.
    SQLite records job state and usage, but never becomes the semantic source of
    truth. This keeps the corpus portable to Neo4j, Obsidian, or another sink.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.records = self.root / "records"
        self.records.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "ledger.sqlite"
        self._initialize_db()

    def path_for(self, table: str) -> Path:
        if table not in TABLE_FILES:
            raise KeyError(f"Unknown record table: {table}")
        return self.records / TABLE_FILES[table]

    def read(self, table: str) -> list[dict[str, Any]]:
        path = self.path_for(table)
        if not path.exists():
            return []
        output: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line.strip():
                    try:
                        output.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"Invalid JSONL in {path}:{line_number}") from exc
        return output

    def by_id(self, table: str) -> dict[str, dict[str, Any]]:
        return {row["id"]: row for row in self.read(table)}

    def upsert(self, table: str, rows: Iterable[dict[str, Any]]) -> int:
        current = self.by_id(table)
        changed = 0
        for row in rows:
            if "id" not in row:
                raise ValueError(f"Every {table} record must contain id")
            if current.get(row["id"]) != row:
                current[row["id"]] = row
                changed += 1
        self._write_all(table, current.values())
        return changed

    def replace(self, table: str, rows: Iterable[dict[str, Any]]) -> None:
        indexed: dict[str, dict[str, Any]] = {}
        for row in rows:
            if "id" not in row:
                raise ValueError(f"Every {table} record must contain id")
            indexed[row["id"]] = row
        self._write_all(table, indexed.values())

    def mark_stage(
        self,
        item_id: str,
        stage: str,
        fingerprint: str,
        status: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                INSERT INTO stage_state(item_id, stage, fingerprint, status, details, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id, stage) DO UPDATE SET
                  fingerprint=excluded.fingerprint,
                  status=excluded.status,
                  details=excluded.details,
                  updated_at=excluded.updated_at
                """,
                (item_id, stage, fingerprint, status, json.dumps(details or {}), now),
            )

    def stage_is_current(self, item_id: str, stage: str, fingerprint: str) -> bool:
        with sqlite3.connect(self.db_path) as db:
            row = db.execute(
                "SELECT status, fingerprint FROM stage_state WHERE item_id=? AND stage=?",
                (item_id, stage),
            ).fetchone()
        return bool(row and row[0] == "SUCCEEDED" and row[1] == fingerprint)

    def clear_stages(self, item_ids: Iterable[str], stages: Iterable[str]) -> None:
        pairs = [(item_id, stage) for item_id in item_ids for stage in stages]
        if not pairs:
            return
        with sqlite3.connect(self.db_path) as db:
            db.executemany(
                "DELETE FROM stage_state WHERE item_id=? AND stage=?",
                pairs,
            )

    def record_usage(
        self,
        provider: str,
        model: str,
        chunk_id: str,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
    ) -> None:
        with sqlite3.connect(self.db_path) as db:
            db.execute(
                """
                INSERT INTO usage(provider, model, chunk_id, input_tokens, output_tokens,
                                  estimated_cost_usd, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    provider,
                    model,
                    chunk_id,
                    input_tokens,
                    output_tokens,
                    estimated_cost_usd,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def _write_all(self, table: str, rows: Iterable[dict[str, Any]]) -> None:
        path = self.path_for(table)
        temp = path.with_suffix(path.suffix + ".tmp")
        ordered = sorted(rows, key=lambda row: row["id"])
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            for row in ordered:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        temp.replace(path)

    def _initialize_db(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS stage_state (
                  item_id TEXT NOT NULL,
                  stage TEXT NOT NULL,
                  fingerprint TEXT NOT NULL,
                  status TEXT NOT NULL,
                  details TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  PRIMARY KEY(item_id, stage)
                );
                CREATE TABLE IF NOT EXISTS usage (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  provider TEXT NOT NULL,
                  model TEXT NOT NULL,
                  chunk_id TEXT NOT NULL,
                  input_tokens INTEGER NOT NULL,
                  output_tokens INTEGER NOT NULL,
                  estimated_cost_usd REAL NOT NULL,
                  recorded_at TEXT NOT NULL
                );
                """
            )
