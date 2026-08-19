from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .db import Database
from .models import canonical_json

GENESIS_HASH = "0" * 64


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def event_digest(prev_hash: str, header: dict[str, Any], payload: object) -> str:
    material = prev_hash + canonical_json(header) + canonical_json(payload)
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked_events: int
    head_hash: str
    errors: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "checked_events": self.checked_events,
            "head_hash": self.head_hash,
            "errors": self.errors,
        }


class EventLedger:
    def __init__(self, database: Database) -> None:
        self.database = database

    def append(
        self,
        *,
        entity_type: str,
        entity_id: str,
        event_type: str,
        payload: dict[str, Any],
        actor: str = "controller",
        connection: sqlite3.Connection | None = None,
        created_at: str | None = None,
    ) -> sqlite3.Row:
        if connection is None:
            with self.database.transaction(immediate=True) as own:
                return self.append(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    event_type=event_type,
                    payload=payload,
                    actor=actor,
                    connection=own,
                    created_at=created_at,
                )
        last = connection.execute(
            "SELECT seq, event_hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = last["event_hash"] if last else GENESIS_HASH
        seq = int(last["seq"]) + 1 if last else 1
        timestamp = created_at or utc_now()
        header = {
            "seq": seq,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor": actor,
            "created_at": timestamp,
        }
        digest = event_digest(prev_hash, header, payload)
        connection.execute(
            """
            INSERT INTO events(
                seq, prev_hash, event_hash, entity_type, entity_id,
                event_type, actor, created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                prev_hash,
                digest,
                entity_type,
                entity_id,
                event_type,
                actor,
                timestamp,
                canonical_json(payload),
            ),
        )
        return connection.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()

    def verify(self) -> VerifyResult:
        errors: list[str] = []
        expected_prev = GENESIS_HASH
        expected_seq = 1
        with self.database.connect() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY seq").fetchall()
        for row in rows:
            seq = int(row["seq"])
            if seq != expected_seq:
                errors.append(f"event sequence gap: expected {expected_seq}, got {seq}")
            if row["prev_hash"] != expected_prev:
                errors.append(f"event {seq} has invalid prev_hash")
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                errors.append(f"event {seq} has invalid payload JSON")
                payload = None
            header = {
                "seq": seq,
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "created_at": row["created_at"],
            }
            actual = event_digest(row["prev_hash"], header, payload)
            if actual != row["event_hash"]:
                errors.append(f"event {seq} hash mismatch")
            expected_prev = row["event_hash"]
            expected_seq = seq + 1
        return VerifyResult(not errors, len(rows), expected_prev, errors)

    def list(self, *, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (after, limit)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]
