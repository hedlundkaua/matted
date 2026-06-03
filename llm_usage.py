from __future__ import annotations

import json
import sqlite3
import time
from typing import Any


def utc_ts_ms() -> int:
    return int(time.time() * 1000)


def ensure_llm_usage_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS llm_usage (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor TEXT NOT NULL,
          context TEXT NOT NULL,
          provider TEXT NOT NULL,
          model TEXT,
          agent TEXT,
          mode TEXT,
          server_url TEXT,
          session_id TEXT,
          tokens_json TEXT NOT NULL DEFAULT '{}',
          total_tokens INTEGER,
          cost REAL,
          raw_json TEXT NOT NULL DEFAULT '{}',
          created_at INTEGER NOT NULL
        );
        """
    )
    con.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_llm_usage_created
        ON llm_usage (created_at, id);
        """
    )


def _numeric_token_total(tokens: Any) -> int | None:
    if isinstance(tokens, dict):
        values = [value for value in tokens.values() if isinstance(value, (int, float))]
        if values:
            return int(sum(values))
    if isinstance(tokens, (int, float)):
        return int(tokens)
    return None


def _provider_mode(provider: Any) -> str:
    mode_fn = getattr(provider, "_mode", None)
    if callable(mode_fn):
        try:
            return str(mode_fn())
        except Exception:
            return ""
    return ""


def extract_provider_usage(provider: Any) -> dict[str, Any] | None:
    step = getattr(provider, "last_step_finish", None)
    if not isinstance(step, dict):
        return None

    tokens = step.get("tokens") or step.get("token") or {}
    cost = step.get("cost")
    if cost is None:
        cost = step.get("costUSD") or step.get("cost_usd")
    try:
        parsed_cost = float(cost) if cost is not None else None
    except (TypeError, ValueError):
        parsed_cost = None

    return {
        "provider": provider.__class__.__name__,
        "model": getattr(provider, "model", None),
        "agent": getattr(provider, "agent", None),
        "mode": _provider_mode(provider),
        "server_url": getattr(provider, "_server_url", None),
        "session_id": getattr(provider, "_session_id", None),
        "tokens": tokens if isinstance(tokens, dict) else {"total": tokens},
        "total_tokens": _numeric_token_total(tokens),
        "cost": parsed_cost,
        "raw": step,
    }


def record_provider_usage(
    con: sqlite3.Connection,
    *,
    actor: str,
    context: str,
    provider: Any,
) -> dict[str, Any] | None:
    usage = extract_provider_usage(provider)
    if not usage:
        return None

    ensure_llm_usage_table(con)
    con.execute(
        """
        INSERT INTO llm_usage (
          actor, context, provider, model, agent, mode, server_url, session_id,
          tokens_json, total_tokens, cost, raw_json, created_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            actor,
            context,
            usage["provider"],
            usage.get("model"),
            usage.get("agent"),
            usage.get("mode"),
            usage.get("server_url"),
            usage.get("session_id"),
            json.dumps(usage.get("tokens") or {}, ensure_ascii=True),
            usage.get("total_tokens"),
            usage.get("cost"),
            json.dumps(usage.get("raw") or {}, ensure_ascii=True),
            utc_ts_ms(),
        ),
    )
    return usage


def usage_summary(usage: dict[str, Any]) -> str:
    pieces = []
    total = usage.get("total_tokens")
    if total is not None:
        pieces.append(f"tokens={total}")
    cost = usage.get("cost")
    if cost is not None:
        pieces.append(f"cost={cost:.6f}")
    session_id = usage.get("session_id")
    if session_id:
        pieces.append(f"sessionID={session_id}")
    return " ".join(pieces) if pieces else "usage recorded"
