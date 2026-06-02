from __future__ import annotations

import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProcessHandle:
    name: str
    pid: int
    backend: str


class SubprocessProcessManager:
    """Small runtime adapter for future Textual-owned subprocess execution."""

    def __init__(self, engine_dir: Path, root: Path, python: str | None = None) -> None:
        self.engine_dir = engine_dir.resolve()
        self.root = root.resolve()
        self.python = python or sys.executable
        self.processes: dict[str, subprocess.Popen[str]] = {}

    def start_agent(self, name: str, system_prompt_file: Path, poll_interval_s: float = 0.2) -> ProcessHandle:
        if name in self.processes and self.processes[name].poll() is None:
            proc = self.processes[name]
            return ProcessHandle(name=name, pid=proc.pid, backend="subprocess")

        proc = subprocess.Popen(
            [
                self.python,
                "-u",
                str(self.engine_dir / "agent_base.py"),
                "--root",
                str(self.root),
                "--name",
                name,
                "--system-prompt-file",
                str(system_prompt_file),
                "--poll",
                str(poll_interval_s),
                "--service",
            ],
            cwd=str(self.root),
            text=True,
            env={**os.environ, "MATTED_UI_BACKEND": "textual", "MATTED_ROOT": str(self.root)},
        )
        self.processes[name] = proc
        return ProcessHandle(name=name, pid=proc.pid, backend="subprocess")

    def stop_agent(self, name: str) -> bool:
        proc = self.processes.pop(name, None)
        if proc is None:
            return False
        if proc.poll() is not None:
            return True
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        return True

    def list_agents(self) -> list[ProcessHandle]:
        handles: list[ProcessHandle] = []
        for name, proc in self.processes.items():
            if proc.poll() is None:
                handles.append(ProcessHandle(name=name, pid=proc.pid, backend="subprocess"))
        return handles


class TmuxProcessManager:
    """Compatibility adapter around an existing tmux-backed master."""

    def __init__(self, master: object) -> None:
        self.master = master

    def stop_agent(self, name: str) -> bool:
        return bool(getattr(self.master, "encerrar_agente")(name))

    def process_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def terminate_pid(self, pid: int) -> None:
        os.kill(pid, signal.SIGTERM)
