import json
import os
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import LLMProvider


DEFAULT_OPENCODE_PATH = "/home/matted/matted/external/opencode"
DEFAULT_OPENCODE_MODEL = "openrouter/openai/gpt-4o-mini"
DEFAULT_OPENCODE_AGENT = "build"
DEFAULT_OPENCODE_TIMEOUT = "300"
DEFAULT_BUN_PATH = "/home/matted/.bun/bin/bun"


class OpenCodeProvider(LLMProvider):
    """
    Provider for OpenCode running as a headless subprocess.
    """

    def __init__(self, root_dir: str):
        self.root_dir = root_dir
        self.opencode_path = os.environ.get("MATTED_OPENCODE_PATH", DEFAULT_OPENCODE_PATH)
        self.model = os.environ.get("MATTED_OPENCODE_MODEL", DEFAULT_OPENCODE_MODEL)
        self.agent = os.environ.get("MATTED_OPENCODE_AGENT", DEFAULT_OPENCODE_AGENT)
        self.timeout = float(os.environ.get("MATTED_OPENCODE_TIMEOUT", DEFAULT_OPENCODE_TIMEOUT))
        self.last_step_finish: Optional[Dict[str, Any]] = None

    def _bun_cmd(self) -> str:
        return DEFAULT_BUN_PATH if Path(DEFAULT_BUN_PATH).is_file() else "bun"

    def _opencode_package_dir(self) -> str:
        return str(Path(self.opencode_path) / "packages" / "opencode")

    def _opencode_cmd(self, prompt: str) -> List[str]:
        return [
            self._bun_cmd(),
            "run",
            "--cwd",
            self._opencode_package_dir(),
            "--conditions=browser",
            "src/index.ts",
            "run",
            "--format",
            "json",
            "--model",
            self.model,
            "--agent",
            self.agent,
            "--dir",
            self.root_dir,
            prompt,
        ]

    def _opencode_config_content(self) -> str:
        return json.dumps(
            {
                "model": self.model,
                "small_model": self.model,
                "permission": {
                    "edit": "deny",
                    "bash": "deny",
                    "webfetch": "deny",
                    "websearch": "deny",
                    "question": "deny",
                },
            }
        )

    def _opencode_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "XDG_DATA_HOME": "/tmp/matted-opencode-data",
                "XDG_CONFIG_HOME": "/tmp/matted-opencode-config",
                "XDG_CACHE_HOME": "/tmp/matted-opencode-cache",
                "XDG_STATE_HOME": "/tmp/matted-opencode-state",
                "OPENCODE_CONFIG_CONTENT": self._opencode_config_content(),
            }
        )
        return env

    def _opencode_db_path(self) -> Path:
        return Path("/tmp/matted-opencode-data") / "opencode" / "opencode-local.db"

    def _tool_progress_text(self, event: Dict[str, Any]) -> str:
        tool_name = event.get("tool") or event.get("name")
        part = event.get("part")
        if not tool_name and isinstance(part, dict):
            tool_name = part.get("tool") or part.get("name")
        return f"[opencode] tool_use: {tool_name}" if tool_name else "[opencode] tool_use"

    def _event_text(self, event: Dict[str, Any]) -> str:
        part = event.get("part")
        if isinstance(part, dict) and part.get("text") is not None:
            return str(part["text"])
        if event.get("text") is not None:
            return str(event["text"])
        return ""

    def _error_message(self, event: Dict[str, Any]) -> str:
        error = event.get("error")
        name = None
        message = None
        if isinstance(error, dict):
            name = error.get("name")
            data = error.get("data")
            if isinstance(data, dict):
                message = data.get("message")
            message = message or error.get("message")
        ref = event.get("ref")
        pieces = ["OpenCode error"]
        if name:
            pieces.append(str(name))
        if message:
            pieces.append(str(message))
        if ref:
            pieces.append(f"ref={ref}")
        return ": ".join(pieces)

    def _handle_event(self, event: Dict[str, Any], chunks: List[str]) -> None:
        event_type = event.get("type")
        if event_type == "text":
            text = self._event_text(event)
            if text:
                self._emit_usage_delta(text)
                self._emit_stream_delta(text)
                chunks.append(text)
        elif event_type == "tool_use":
            self._emit_stream_delta(self._tool_progress_text(event))
        elif event_type == "step_finish":
            self.last_step_finish = event
        elif event_type == "error":
            raise RuntimeError(self._error_message(event))

    def _fallback_text_from_db(self, started_at_ms: int) -> str:
        db_path = self._opencode_db_path()
        if not db_path.is_file():
            return ""

        uri = f"file:{db_path}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                select p.time_created, p.data as part_data, m.data as message_data, s.directory
                from part p
                join message m on m.id = p.message_id
                join session s on s.id = p.session_id
                where p.time_created >= ?
                order by p.time_created desc
                limit 40
                """,
                (started_at_ms,),
            ).fetchall()
        except sqlite3.Error:
            return ""
        finally:
            try:
                conn.close()
            except Exception:
                pass

        for row in rows:
            if row["directory"] != self.root_dir:
                continue
            try:
                message = json.loads(row["message_data"] or "{}")
                part = json.loads(row["part_data"] or "{}")
            except json.JSONDecodeError:
                continue
            if message.get("role") != "assistant":
                continue
            if part.get("type") != "text":
                continue
            text = part.get("text")
            if text:
                return str(text).strip()
        return ""

    def generate(self, prompt: str) -> str:
        cmd = self._opencode_cmd(prompt)
        started_at_ms = int(time.time() * 1000)
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.root_dir,
            env=self._opencode_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        chunks: List[str] = []
        recent_events: List[str] = []
        stderr_lines: List[str] = []
        reader_errors: List[Exception] = []

        def read_stderr() -> None:
            if process.stderr is None:
                return
            try:
                for line in process.stderr:
                    stderr_lines.append(line)
            finally:
                process.stderr.close()

        def read_stdout() -> None:
            if process.stdout is None:
                return
            try:
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        recent_events.append(line)
                        del recent_events[:-5]
                        self._handle_event(event, chunks)
            except Exception as exc:
                reader_errors.append(exc)
                if process.poll() is None:
                    process.kill()
            finally:
                process.stdout.close()

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread.start()
        stdout_thread.start()
        try:
            return_code = process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise RuntimeError(f"OpenCode timed out after {self.timeout:g}s") from exc
        except Exception:
            if process.poll() is None:
                process.kill()
            raise
        finally:
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)

        if reader_errors:
            raise reader_errors[0]

        stderr = "".join(stderr_lines)

        if return_code != 0:
            event_tail = "\n".join(recent_events)
            details = stderr.strip()
            if event_tail:
                details = f"{details}\nRecent events:\n{event_tail}" if details else f"Recent events:\n{event_tail}"
            raise RuntimeError(f"OpenCode exited with code {return_code}: {details}".strip())

        output = "".join(chunks).strip()
        if output:
            return output

        fallback = self._fallback_text_from_db(started_at_ms)
        if fallback:
            self._emit_usage_delta(fallback)
            self._emit_stream_delta(fallback)
            return fallback

        event_tail = "\n".join(recent_events)
        details = stderr.strip()
        if event_tail:
            details = f"{details}\nRecent events:\n{event_tail}" if details else f"Recent events:\n{event_tail}"
        raise RuntimeError(f"OpenCode produced no text output: {details}".strip())
