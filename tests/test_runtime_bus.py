from pathlib import Path

import sqlite3

from init_bus import init_db
from runtime_bus import SQLiteCommandBus, SQLiteEventBus


def test_init_db_creates_runtime_tables(tmp_path: Path) -> None:
    init_db(tmp_path)
    init_db(tmp_path)

    con = sqlite3.connect(tmp_path / "squad.db")
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        con.close()

    assert {"ui_events", "ui_commands", "agent_processes"} <= tables


def test_sqlite_event_bus_drains_incrementally(tmp_path: Path) -> None:
    init_db(tmp_path)
    bus = SQLiteEventBus(tmp_path)

    first_id = bus.emit("log", "master", "one")
    second_id = bus.emit("status", "worker", "two", status="RUN", elapsed_s=3)

    first_batch = bus.drain_after(0)
    second_batch = bus.drain_after(first_id)

    assert [event.id for event in first_batch] == [first_id, second_id]
    assert [event.id for event in second_batch] == [second_id]
    assert second_batch[0].metadata["status"] == "RUN"


def test_sqlite_command_bus_claims_once_and_acks(tmp_path: Path) -> None:
    init_db(tmp_path)
    bus = SQLiteCommandBus(tmp_path)
    command_id = bus.send_message("master", "ola")

    command = bus.claim_next("master")
    duplicate = bus.claim_next("master")
    assert command is not None
    assert command.id == command_id
    assert command.payload == {"text": "ola"}
    assert duplicate is None

    bus.ack(command_id)

    con = sqlite3.connect(tmp_path / "squad.db")
    try:
        row = con.execute("SELECT status FROM ui_commands WHERE id=?", (command_id,)).fetchone()
    finally:
        con.close()

    assert row[0] == "done"

