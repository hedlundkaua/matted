from __future__ import annotations

import os
import queue
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runtime_bus import RuntimeEvent, SQLiteEventBus


@dataclass
class UIEvent:
    type: str
    agent: str
    message: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


_EVENTS: queue.Queue[UIEvent] = queue.Queue()


def textual_enabled() -> bool:
    return os.environ.get("MATTED_UI_BACKEND", "").strip().lower() == "textual"


def emit_event(event_type: str, agent: str, message: str, **metadata: Any) -> None:
    if not textual_enabled():
        return
    root = os.environ.get("MATTED_ROOT")
    if root:
        try:
            SQLiteEventBus(Path(root)).emit(event_type, agent, message, **metadata)
            return
        except Exception:
            pass
    _EVENTS.put(UIEvent(type=event_type, agent=agent, message=message, metadata=metadata))


def drain_events(limit: int = 200) -> list[UIEvent]:
    events: list[UIEvent] = []
    for _ in range(limit):
        try:
            events.append(_EVENTS.get_nowait())
        except queue.Empty:
            break
    return events


def drain_sqlite_events(root: Path, after_id: int = 0, limit: int = 200) -> list[RuntimeEvent]:
    return SQLiteEventBus(root).drain_after(after_id=after_id, limit=limit)
