#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import re
import signal
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from init_bus import init_db
from runtime_bus import RuntimeEvent, SQLiteCommandBus
from ui_events import drain_events, drain_sqlite_events


DEFAULT_TUI_EVENT_INTERVAL_S = "0.35"
DEFAULT_TUI_INITIAL_HISTORY_LIMIT = "80"
DEFAULT_TUI_EVENT_BATCH_LIMIT = "80"
DEFAULT_TUI_LOG_MAX_LINES = "1200"


try:
    if sys.version_info < (3, 9):
        raise ModuleNotFoundError("Textual atual requer Python 3.9+")
    from textual.app import App, ComposeResult
    from textual.containers import Container, Grid, Horizontal
    from textual.widgets import Footer, Header, Input, RichLog, Static
except ModuleNotFoundError as exc:  # pragma: no cover - exercised manually.
    raise SystemExit(
        "Textual nao esta instalado neste Python. Recrie o venv com Python 3.9+ e instale dependencias "
        "ou use MATTED_UI_BACKEND=tmux."
    ) from exc


class AgentPanel(Container):
    """Painel de chat com header fixo + corpo scrollável."""

    PALETTE = ("red", "green", "yellow", "blue", "magenta", "cyan")

    def __init__(self, agent: str) -> None:
        super().__init__(id=f"panel-{agent}")
        self.agent = agent
        self._status = "Idle"
        self._elapsed = 0
        self._tokens = "0"
        self._last_header = ""
        self.badge_style = self._badge_style(agent)
        self._header = Static("", id=f"header-{agent}")
        self._body = RichLog(
            id=f"body-{agent}",
            highlight=True,
            auto_scroll=True,
            wrap=True,
        )
        self._refresh_header()

    def _badge_style(self, agent: str) -> str:
        index = sum(ord(ch) for ch in agent) % len(self.PALETTE)
        return f"black on {self.PALETTE[index]}"

    def compose(self) -> ComposeResult:
        yield self._header
        yield self._body

    def _refresh_header(self) -> None:
        header = (
            f"[dim]{self._status} {self._elapsed}s | tok~{self._tokens}[/dim]  "
            f"[cyan]{'─' * 20}[/cyan] [{self.badge_style}] #{self.agent} [/]"
        )
        if header == self._last_header:
            return
        self._last_header = header
        self._header.update(header)

    def append(self, kind: str, message: str) -> None:
        prefix = "" if kind == "output" else f"[{kind}] "
        max_lines = self._log_max_lines()
        lines = str(message or "").splitlines() or [""]
        if len(lines) > max_lines:
            omitted = len(lines) - max_lines
            lines = lines[:max_lines] + [f"[truncated: {omitted} more line(s)]"]
        for line in lines:
            self._body.write(prefix + line)

    def _log_max_lines(self) -> int:
        try:
            return max(100, int(os.environ.get("MATTED_TUI_LOG_MAX_LINES", DEFAULT_TUI_LOG_MAX_LINES)))
        except ValueError:
            return int(DEFAULT_TUI_LOG_MAX_LINES)

    def set_status(self, status: str) -> None:
        old = (self._status, self._elapsed, self._tokens)
        self._status = status
        # tenta extrair elapsed e tokens do formato "RUN 12s | tok~450"
        import re
        m = re.search(r"(\d+)s\s*\|\s*tok~(\S+)", status)
        if m:
            self._elapsed = int(m.group(1))
            self._tokens = m.group(2)
            self._status = status.split()[0]  # RUN / IDLE / etc
        if old != (self._status, self._elapsed, self._tokens):
            self._refresh_header()


class MattedTUI(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #panels {
        height: 1fr;
    }
    #agent-grid {
        grid-size: 2;
        grid-gutter: 1 2;
        height: 1fr;
    }
    AgentPanel {
        border: solid $accent;
        padding: 0 1;
        overflow-x: hidden;
    }
    AgentPanel.focused {
        border: heavy $success;
    }
    AgentPanel > Static {
        height: 1;
        width: 100%;
        overflow: hidden;
    }
    AgentPanel > RichLog {
        height: 1fr;
        overflow-x: hidden;
        overflow-y: auto;
        min-height: 1;
    }
    #input-row {
        height: 3;
    }
    #target {
        width: 24;
        content-align: center middle;
        border: solid $accent;
    }
    #target.active {
        border: heavy $success;
    }
    #prompt {
        width: 1fr;
    }
    """

    BINDINGS = [
        ("tab", "next_agent", "Next agent"),
        ("shift+tab", "previous_agent", "Previous agent"),
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self, root: Path, poll_interval_s: float = 0.2) -> None:
        super().__init__()
        self.root = root.resolve()
        self.poll_interval_s = poll_interval_s
        self.engine_dir = Path(__file__).resolve().parent
        self.master_process: subprocess.Popen[str] | None = None
        self.process_logs: list[Any] = []
        self.active_agents: set[str] = set()
        self.panels: dict[str, AgentPanel] = {}
        self.focus_order: list[str] = ["master"]
        self.focus_index = 0
        self.command_bus: SQLiteCommandBus | None = None
        self.pending_events: dict[str, list[RuntimeEvent]] = defaultdict(list)
        self.last_event_id = 0
        self._last_target = ""

    def compose(self) -> ComposeResult:
        yield Header()
        with Container(id="panels"):
            yield Grid(id="agent-grid")
        with Horizontal(id="input-row"):
            yield Static("#master", id="target")
            yield Input(placeholder="Digite uma mensagem para o painel focado", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        os.environ["MATTED_UI_BACKEND"] = "textual"
        os.environ["MATTED_ROOT"] = str(self.root)
        init_db(self.root)
        self.command_bus = SQLiteCommandBus(self.root)
        self._ensure_panel("master")
        self._load_history()
        self._load_active_agents()
        self.last_event_id = self._current_event_id()
        self._start_master_process()
        self.set_interval(self._event_interval_s(), self._drain_ui_events)
        self.query_one("#prompt", Input).focus()

    def _event_interval_s(self) -> float:
        try:
            return max(0.2, float(os.environ.get("MATTED_TUI_EVENT_INTERVAL", DEFAULT_TUI_EVENT_INTERVAL_S)))
        except ValueError:
            return float(DEFAULT_TUI_EVENT_INTERVAL_S)

    def _initial_history_limit(self) -> int:
        try:
            return max(0, int(os.environ.get("MATTED_TUI_INITIAL_HISTORY_LIMIT", DEFAULT_TUI_INITIAL_HISTORY_LIMIT)))
        except ValueError:
            return int(DEFAULT_TUI_INITIAL_HISTORY_LIMIT)

    def _event_batch_limit(self) -> int:
        try:
            return max(20, int(os.environ.get("MATTED_TUI_EVENT_BATCH_LIMIT", DEFAULT_TUI_EVENT_BATCH_LIMIT)))
        except ValueError:
            return int(DEFAULT_TUI_EVENT_BATCH_LIMIT)

    def _start_master_process(self) -> None:
        log_dir = self.root / ".matted" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = (log_dir / "master-textual.log").open("a", encoding="utf-8")
        self.process_logs.append(log_file)
        env = {
            **os.environ,
            "MATTED_UI_BACKEND": "textual",
            "MATTED_ROOT": str(self.root),
        }
        self.master_process = subprocess.Popen(
            [
                sys.executable,
                "-u",
                str(self.engine_dir / "master.py"),
                "--root",
                str(self.root),
                "--poll",
                str(self.poll_interval_s),
                "--service",
            ],
            cwd=str(self.root),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

    def _current_event_id(self) -> int:
        db_path = self.root / "squad.db"
        if not db_path.exists():
            return 0
        con = sqlite3.connect(db_path)
        try:
            row = con.execute("SELECT COALESCE(MAX(id), 0) FROM ui_events").fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            con.close()

    def _load_history(self) -> None:
        db_path = self.root / "squad.db"
        if not db_path.exists():
            return
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            limit = self._initial_history_limit()
            if limit <= 0:
                return
            rows = con.execute(
                """
                SELECT autor, mensagem
                FROM (
                    SELECT id, autor, mensagem
                    FROM historico
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                author = str(row["autor"] or "master")
                message = str(row["mensagem"] or "")
                if "->" in author:
                    _, target = author.split("->", 1)
                    panel = self._ensure_panel(target)
                    panel.append("user", message)
                elif author == "usuario":
                    self._ensure_panel("master").append("user", message)
                else:
                    self._ensure_panel(author).append("history", message)

            tasks = con.execute(
                """
                SELECT agente_destino, status, solicitacao, resposta
                FROM (
                    SELECT id, agente_destino, status, solicitacao, resposta
                    FROM tarefas
                    ORDER BY id DESC
                    LIMIT ?
                )
                ORDER BY id ASC
                """,
                (limit,),
            ).fetchall()
            for task in tasks:
                agent = str(task["agente_destino"] or "")
                if not agent:
                    continue
                panel = self._ensure_panel(agent)
                panel.append("task", f"{task['status']}: {task['solicitacao']}")
                if task["resposta"]:
                    panel.append("result", str(task["resposta"]))
        finally:
            con.close()

    def _load_active_agents(self) -> None:
        db_path = self.root / "squad.db"
        if not db_path.exists():
            return
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT agent FROM agent_processes WHERE status='running' ORDER BY agent"
            ).fetchall()
        except sqlite3.OperationalError:
            return
        finally:
            con.close()
        for row in rows:
            agent = str(row["agent"] or "")
            if agent:
                self.active_agents.add(agent)
                self._ensure_panel(agent)

    def _ensure_panel(self, agent: str) -> AgentPanel:
        if agent in self.panels:
            return self.panels[agent]
        panel = AgentPanel(agent)
        self.panels[agent] = panel
        self.focus_order.append(agent) if agent != "master" and agent not in self.focus_order else None
        self.query_one("#agent-grid", Grid).mount(panel)
        for event in self.pending_events.pop(agent, []):
            self._apply_event(event)
        return panel

    def _remove_panel(self, agent: str) -> None:
        panel = self.panels.pop(agent, None)
        if panel:
            try:
                panel.remove()
            except Exception:
                pass
        self.active_agents.discard(agent)
        self.focus_order = [name for name in self.focus_order if name != agent]
        if self.focus_order:
            self.focus_index = min(self.focus_index, len(self.focus_order) - 1)
        else:
            self.focus_index = 0
            self.focus_order = ["master"]
        self._update_target()
        self.pending_events.pop(agent, None)

    def _apply_event(self, event: RuntimeEvent) -> None:
        if event.type == "agent_spawned":
            self.active_agents.add(event.agent)
            self._ensure_panel(event.agent)
        elif event.type == "agent_closed":
            self.active_agents.discard(event.agent)
            self._remove_panel(event.agent)
            return
        panel = self.panels.get(event.agent)
        if panel is None:
            self.pending_events[event.agent].append(event)
            return
        if event.type == "status":
            if event.agent != "master":
                self.active_agents.add(event.agent)
            status = event.metadata.get("status", "IDLE")
            elapsed = event.metadata.get("elapsed_s", 0)
            token_label = event.metadata.get("token_label", "0")
            panel.set_status(f"{status} {elapsed}s | tok~{token_label}")
        elif event.type == "tool":
            short = event.metadata.get("tool", "?")
            panel.append("tool", f"[bold yellow]⚡ {short}[/bold yellow]  {event.message}")
        elif event.type == "cmd_out":
            rc = event.metadata.get("returncode", 0)
            color = "green" if rc == 0 else "red"
            panel.append("cmd_out", f"[{color}]{event.message}[/{color}]")
        elif event.type == "llm_stream":
            panel.append("live", f"[dim]{event.message}[/dim]")
        elif event.type == "log":
            panel.append("log", f"[dim]{event.message}[/dim]")
        else:
            panel.append(event.type, event.message)

    def _drain_ui_events(self) -> None:
        changed = False
        sqlite_events = drain_sqlite_events(self.root, after_id=self.last_event_id, limit=self._event_batch_limit())
        for event in sqlite_events:
            if event.id is not None:
                self.last_event_id = max(self.last_event_id, event.id)
            self._apply_event(event)
            changed = True
        for event in drain_events(limit=self._event_batch_limit()):
            self._apply_event(
                RuntimeEvent(
                    type=event.type,
                    agent=event.agent,
                    message=event.message,
                    timestamp=event.timestamp,
                    metadata=event.metadata,
                )
            )
            changed = True
        if changed:
            self._update_target()

    def _update_target(self) -> None:
        target = self.focus_order[self.focus_index] if self.focus_order else "master"
        if target == self._last_target:
            return
        self._last_target = target
        # highlight active panel
        for name, panel in self.panels.items():
            if name == target:
                panel.add_class("focused")
            else:
                panel.remove_class("focused")
        # update target label
        target_widget = self.query_one("#target", Static)
        target_widget.update(f"#{target}")
        target_widget.add_class("active")

    def action_next_agent(self) -> None:
        if self.focus_order:
            self.focus_index = (self.focus_index + 1) % len(self.focus_order)
            self._update_target()

    def action_previous_agent(self) -> None:
        if self.focus_order:
            self.focus_index = (self.focus_index - 1) % len(self.focus_order)
            self._update_target()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text or self.command_bus is None:
            return
        target = self.focus_order[self.focus_index] if self.focus_order else "master"

        # /kill <agente> — encerra agente diretamente
        kill_match = re.match(r"^/kill\s+([A-Za-z0-9_-]+)\s*$", text)
        if kill_match:
            agent_name = kill_match.group(1)
            panel = self._ensure_panel("master")
            panel.append("user", f"> /kill {agent_name}")
            if agent_name not in self.active_agents:
                panel.append("system", f"Agente '{agent_name}' não está ativo. Ativos: {', '.join(sorted(self.active_agents)) or 'nenhum'}")
                return
            self.command_bus.send_message("master", f"kill {agent_name}")
            return

        # /to <agente> <msg> — redireciona mensagem
        route_match = re.match(r"^/(?:to|para)\s+([A-Za-z0-9_-]+)\s+(.+)$", text, flags=re.DOTALL)
        if route_match:
            requested = route_match.group(1)
            text = route_match.group(2).strip()
            if requested == "master" or requested in self.active_agents:
                target = requested
                if target not in self.focus_order:
                    self.focus_order.append(target)
                self.focus_index = self.focus_order.index(target)
                self._update_target()
        panel = self._ensure_panel(target)
        panel.append("user", f"> {text}")
        if target != "master" and target not in self.active_agents:
            panel.append("system", "Agente nao esta ativo.")
            return
        self.command_bus.send_message(target, text)

    def on_unmount(self) -> None:
        if self.master_process is not None and self.master_process.poll() is None:
            try:
                os.killpg(self.master_process.pid, signal.SIGTERM)
                self.master_process.wait(timeout=5)
            except Exception:
                try:
                    os.killpg(self.master_process.pid, signal.SIGKILL)
                except Exception:
                    pass
        for handle in self.process_logs:
            try:
                handle.close()
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--poll", type=float, default=0.2)
    args = parser.parse_args()
    MattedTUI(root=Path(args.root), poll_interval_s=args.poll).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
