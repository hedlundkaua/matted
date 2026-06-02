from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def utc_ts_ms() -> int:
    return int(time.time() * 1000)


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    return con


@dataclass
class RuntimeEvent:
    type: str
    agent: str
    message: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    id: int | None = None


@dataclass
class RuntimeCommand:
    id: int
    target: str
    type: str
    payload: dict[str, Any]
    status: str
    created_at: int


class SQLiteEventBus:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.db_path = self.root / "squad.db"

    def emit(self, event_type: str, agent: str, message: str, **metadata: Any) -> int:
        con = connect(self.db_path)
        try:
            cur = con.execute(
                """
                INSERT INTO ui_events (type, agent, message, metadata_json, created_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    event_type,
                    agent,
                    str(message or ""),
                    json.dumps(metadata, ensure_ascii=True),
                    utc_ts_ms(),
                ),
            )
            return int(cur.lastrowid)
        finally:
            con.close()

    def drain_after(self, after_id: int = 0, limit: int = 200) -> list[RuntimeEvent]:
        con = connect(self.db_path)
        try:
            rows = con.execute(
                """
                SELECT id, type, agent, message, metadata_json, created_at
                FROM ui_events
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (after_id, limit),
            ).fetchall()
        finally:
            con.close()

        events: list[RuntimeEvent] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            events.append(
                RuntimeEvent(
                    id=int(row["id"]),
                    type=str(row["type"]),
                    agent=str(row["agent"]),
                    message=str(row["message"]),
                    timestamp=int(row["created_at"]) / 1000,
                    metadata=metadata if isinstance(metadata, dict) else {},
                )
            )
        return events


class SQLiteCommandBus:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.db_path = self.root / "squad.db"

    def send(self, target: str, command_type: str, payload: dict[str, Any] | None = None) -> int:
        con = connect(self.db_path)
        try:
            cur = con.execute(
                """
                INSERT INTO ui_commands (target, type, payload_json, status, created_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    target,
                    command_type,
                    json.dumps(payload or {}, ensure_ascii=True),
                    "pending",
                    utc_ts_ms(),
                ),
            )
            return int(cur.lastrowid)
        finally:
            con.close()

    def send_message(self, target: str, text: str) -> int:
        return self.send(target, "message", {"text": text})

    def claim_next(self, target: str) -> RuntimeCommand | None:
        con = connect(self.db_path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """
                SELECT id, target, type, payload_json, status, created_at
                FROM ui_commands
                WHERE target=? AND status='pending'
                ORDER BY id ASC
                LIMIT 1
                """,
                (target,),
            ).fetchone()
            if row is None:
                con.execute("COMMIT")
                return None
            command_id = int(row["id"])
            cur = con.execute(
                """
                UPDATE ui_commands
                SET status='claimed', claimed_at=?
                WHERE id=? AND status='pending'
                """,
                (utc_ts_ms(), command_id),
            )
            if cur.rowcount != 1:
                con.execute("ROLLBACK")
                return None
            con.execute("COMMIT")
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()

        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        return RuntimeCommand(
            id=command_id,
            target=str(row["target"]),
            type=str(row["type"]),
            payload=payload if isinstance(payload, dict) else {},
            status="claimed",
            created_at=int(row["created_at"]),
        )

    def ack(self, command_id: int) -> None:
        self._finish(command_id, "done", None)

    def fail(self, command_id: int, error: str) -> None:
        self._finish(command_id, "failed", error)

    def _finish(self, command_id: int, status: str, error: str | None) -> None:
        con = connect(self.db_path)
        try:
            con.execute(
                """
                UPDATE ui_commands
                SET status=?, completed_at=?, error=?
                WHERE id=?
                """,
                (status, utc_ts_ms(), error, command_id),
            )
        finally:
            con.close()

