#!/usr/bin/env python3

"""Initialize the local SQLite message bus.

Creates (under --root):
  squad.db  (SQLite)

Tables:
  projeto(id, status_global, tecnologias, ultima_atualizacao)
  tarefas(id, agente_destino, status, solicitacao, resposta, data_criacao, master_tratada)
  historico(id, autor, mensagem, timestamp)
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path


def parse_agents(s: str) -> list[str]:
    # Kept for backward compatibility with start_squad.sh args.
    agents = [a.strip() for a in s.split(",") if a.strip()]
    if not agents:
        raise argparse.ArgumentTypeError("--agents cannot be empty")
    return agents


def init_db(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    db_path = root / "squad.db"

    con = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    try:
        # Concurrency-friendly defaults for multi-process polling.
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA busy_timeout=5000;")
        con.execute("PRAGMA foreign_keys=ON;")

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS projeto (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              status_global TEXT NOT NULL,
              tecnologias TEXT NOT NULL,
              ultima_atualizacao INTEGER NOT NULL
            );
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tarefas (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              agente_destino TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('pendente','processando','concluido','erro','aguardando_aprovacao','cancelado')),
              solicitacao TEXT NOT NULL,
              resposta TEXT,
              data_criacao INTEGER NOT NULL,
              master_tratada INTEGER NOT NULL DEFAULT 0 CHECK (master_tratada IN (0,1))
            );
            """
        )
        migrate_tarefas_table(con)
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tarefas_lookup
            ON tarefas (agente_destino, status, data_criacao, id);
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tarefas_master_lookup
            ON tarefas (status, master_tratada, id);
            """
        )

        con.execute(
            """
            CREATE TABLE IF NOT EXISTS historico (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              autor TEXT NOT NULL,
              mensagem TEXT NOT NULL,
              timestamp INTEGER NOT NULL
            );
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_historico_ts ON historico (timestamp, id);")

        now = int(time.time() * 1000)
        con.execute(
            """
            INSERT INTO projeto (id, status_global, tecnologias, ultima_atualizacao)
            VALUES (1, 'Planejamento', '[]', ?)
            ON CONFLICT(id) DO NOTHING;
            """,
            (now,),
        )
    finally:
        con.close()

    return db_path


def migrate_tarefas_table(con: sqlite3.Connection) -> None:
    """Bring existing tarefas tables to the current schema."""
    table = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tarefas'"
    ).fetchone()
    if table is None:
        return

    table_sql = table[0] or ""
    columns = [row[1] for row in con.execute("PRAGMA table_info(tarefas)").fetchall()]
    if "master_tratada" not in columns:
        print("[init_bus] Migrating tarefas: adding master_tratada column")
        con.execute(
            "ALTER TABLE tarefas ADD COLUMN master_tratada INTEGER NOT NULL DEFAULT 0 CHECK (master_tratada IN (0,1))"
        )
    mark_previously_handled_tasks(con)

    required_statuses = ("'erro'", "'aguardando_aprovacao'", "'cancelado'")
    if all(status in table_sql for status in required_statuses):
        return

    print("[init_bus] Migrating tarefas: rebuilding status CHECK to include approval statuses")
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("DROP INDEX IF EXISTS idx_tarefas_lookup")
        con.execute("DROP INDEX IF EXISTS idx_tarefas_master_lookup")
        con.execute("ALTER TABLE tarefas RENAME TO tarefas_old")
        con.execute(
            """
            CREATE TABLE tarefas (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              agente_destino TEXT NOT NULL,
              status TEXT NOT NULL CHECK (status IN ('pendente','processando','concluido','erro','aguardando_aprovacao','cancelado')),
              solicitacao TEXT NOT NULL,
              resposta TEXT,
              data_criacao INTEGER NOT NULL,
              master_tratada INTEGER NOT NULL DEFAULT 0 CHECK (master_tratada IN (0,1))
            );
            """
        )
        con.execute(
            """
            INSERT INTO tarefas (id, agente_destino, status, solicitacao, resposta, data_criacao, master_tratada)
            SELECT id, agente_destino, status, solicitacao, resposta, data_criacao, master_tratada
            FROM tarefas_old;
            """
        )
        con.execute("DROP TABLE tarefas_old")
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def mark_previously_handled_tasks(con: sqlite3.Connection) -> None:
    hist = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='historico'"
    ).fetchone()
    if hist is None:
        return

    cur = con.execute(
        """
        UPDATE tarefas
        SET master_tratada=1
        WHERE status='concluido'
          AND master_tratada=0
          AND EXISTS (
            SELECT 1
            FROM historico
            WHERE historico.autor='master'
              AND instr(historico.mensagem, 'Recebi conclusao da tarefa #' || tarefas.id) > 0
          );
        """
    )
    if cur.rowcount:
        print(f"[init_bus] Migrating tarefas: marked {cur.rowcount} previously handled task(s)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="workspace", help="Workspace root (default: workspace)")
    ap.add_argument("--agents", type=parse_agents, default=["backend", "frontend"], help="Unused (compatibility)")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    db_path = init_db(root)
    print(f"[init_bus] Initialized SQLite bus at: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
