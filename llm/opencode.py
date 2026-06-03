import json
import os
import atexit
import base64
import secrets
import socket
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .base import LLMProvider


DEFAULT_OPENCODE_PATH = "/home/matted/matted/external/opencode"
DEFAULT_OPENCODE_MODEL = "openrouter/openai/gpt-4o-mini"
DEFAULT_OPENCODE_AGENT = "build"
DEFAULT_OPENCODE_TIMEOUT = "300"
DEFAULT_BUN_PATH = "/home/matted/.bun/bin/bun"
DEFAULT_OPENCODE_MODE = "serve"
DEFAULT_OPENCODE_RUN_FALLBACK = "1"
DEFAULT_OPENCODE_PORT = "0"
DENIED_OPENCODE_TOOLS = {
    "bash": False,
    "edit": False,
    "glob": False,
    "grep": False,
    "lsp": False,
    "question": False,
    "read": False,
    "repo_clone": False,
    "repo_overview": False,
    "skill": False,
    "task": False,
    "todowrite": False,
    "webfetch": False,
    "websearch": False,
    "write": False,
}


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
        self.port = os.environ.get("MATTED_OPENCODE_PORT", DEFAULT_OPENCODE_PORT)
        self.server_username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
        self.server_password = os.environ.get("OPENCODE_SERVER_PASSWORD") or secrets.token_urlsafe(24)
        self.last_step_finish: Optional[Dict[str, Any]] = None
        self._server_process: Optional[subprocess.Popen] = None
        self._server_url: Optional[str] = None
        self._server_stdout: List[str] = []
        self._server_stderr: List[str] = []
        self._recent_sse_events: List[str] = []
        self._session_id: Optional[str] = None
        self._last_text: str = ""
        self._text_parts: Dict[str, str] = {}
        self._start_lock = threading.Lock()
        atexit.register(self._teardown_server)

    def _mode(self) -> str:
        mode = os.environ.get("MATTED_OPENCODE_MODE", DEFAULT_OPENCODE_MODE).strip().lower()
        return mode if mode in {"serve", "run"} else DEFAULT_OPENCODE_MODE

    def _serve_enabled(self) -> bool:
        return self._mode() == "serve"

    def _run_fallback_enabled(self) -> bool:
        return os.environ.get("MATTED_OPENCODE_RUN_FALLBACK", DEFAULT_OPENCODE_RUN_FALLBACK).strip() not in {
            "0",
            "false",
            "no",
            "off",
        }

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

    def _opencode_serve_cmd(self) -> List[str]:
        cmd = [
            self._bun_cmd(),
            "run",
            "--cwd",
            self._opencode_package_dir(),
            "--conditions=browser",
            "src/index.ts",
            "serve",
            "--hostname",
            "127.0.0.1",
        ]
        port = self._serve_port()
        if port:
            cmd.extend(["--port", port])
        return cmd

    def _serve_port(self) -> str:
        if self.port and self.port != "0":
            return self.port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return str(sock.getsockname()[1])

    def _opencode_config_content(self) -> str:
        return json.dumps(
            {
                "model": self.model,
                "small_model": self.model,
                "permission": {
                    "bash": "deny",
                    "edit": "deny",
                    "glob": "deny",
                    "grep": "deny",
                    "lsp": "deny",
                    "webfetch": "deny",
                    "websearch": "deny",
                    "question": "deny",
                    "read": "deny",
                    "repo_clone": "deny",
                    "repo_overview": "deny",
                    "skill": "deny",
                    "task": "deny",
                    "todowrite": "deny",
                    "write": "deny",
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
                "OPENCODE_SERVER_USERNAME": self.server_username,
                "OPENCODE_SERVER_PASSWORD": self.server_password,
            }
        )
        return env

    def _opencode_db_path(self) -> Path:
        return Path("/tmp/matted-opencode-data") / "opencode" / "opencode-local.db"

    def _opencode_log_path(self) -> Path:
        return Path("/tmp/matted-opencode-data") / "opencode" / "log" / "dev.log"

    def _log_progress(self, text: str) -> None:
        self._emit_stream_delta(text)

    def _tool_progress_text(self, event: Dict[str, Any]) -> str:
        tool_name = event.get("tool") or event.get("name")
        part = event.get("part")
        if not tool_name and isinstance(part, dict):
            tool_name = part.get("tool") or part.get("name")
        return f"[opencode] tool_use: {tool_name}" if tool_name else "[opencode] tool_use"

    def _step_finish_progress_text(self, event: Dict[str, Any]) -> str:
        tokens = event.get("tokens") if isinstance(event, dict) else None
        cost = event.get("cost") if isinstance(event, dict) else None
        pieces = ["[opencode] step_finish"]
        if isinstance(tokens, dict):
            numeric_values = [value for value in tokens.values() if isinstance(value, (int, float))]
            if numeric_values:
                pieces.append(f"tokens={int(sum(numeric_values))}")
        elif isinstance(tokens, (int, float)):
            pieces.append(f"tokens={int(tokens)}")
        if cost is not None:
            pieces.append(f"cost={cost}")
        return " ".join(pieces)

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

    def _event_properties(self, event: Dict[str, Any]) -> Dict[str, Any]:
        properties = event.get("properties")
        return properties if isinstance(properties, dict) else event

    def _event_session_id(self, event: Dict[str, Any]) -> Optional[str]:
        properties = self._event_properties(event)
        for key in ("sessionID", "sessionId", "session_id"):
            value = properties.get(key)
            if value:
                return str(value)
        session = properties.get("session")
        if isinstance(session, dict):
            value = session.get("id") or session.get("sessionID") or session.get("sessionId")
            if value:
                return str(value)
        message = properties.get("message")
        if isinstance(message, dict):
            value = message.get("sessionID") or message.get("sessionId") or message.get("session_id")
            if value:
                return str(value)
        part = properties.get("part")
        if isinstance(part, dict):
            value = part.get("sessionID") or part.get("sessionId") or part.get("session_id")
            if value:
                return str(value)
        return None

    def _server_stderr_tail(self) -> str:
        return "".join(self._server_stderr[-20:]).strip()

    def _server_stdout_tail(self) -> str:
        return "".join(self._server_stdout[-20:]).strip()

    def _opencode_log_tail(self) -> str:
        log_path = self._opencode_log_path()
        if not log_path.is_file():
            return ""
        try:
            return "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]).strip()
        except OSError:
            return ""

    def _error_context(self, mode: str, message: str) -> str:
        context = [f"mode={mode}"]
        if self._server_url:
            context.append(f"url={self._server_url}")
        if self._session_id:
            context.append(f"sessionID={self._session_id}")
        pieces = [f"{message} ({' '.join(context)})"]
        stderr = self._server_stderr_tail()
        if stderr:
            pieces.append(f"Server stderr:\n{stderr}")
        log_tail = self._opencode_log_tail()
        if log_tail:
            pieces.append(f"OpenCode log:\n{log_tail}")
        if self._recent_sse_events:
            pieces.append("Recent SSE events:\n" + "\n".join(self._recent_sse_events[-5:]))
        return "\n".join(pieces)

    def _teardown_server(self) -> None:
        process = self._server_process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    def _consume_pipe(self, pipe: Any, sink: List[str], detect_url: bool = False) -> None:
        try:
            for line in pipe:
                sink.append(line)
                del sink[:-100]
                if detect_url and "opencode server listening on " in line:
                    self._server_url = line.split("opencode server listening on ", 1)[1].strip()
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def _start_server_process(self) -> None:
        cmd = self._opencode_serve_cmd()
        self._server_stdout = []
        self._server_stderr = []
        self._server_url = None
        self._server_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.root_dir,
            env=self._opencode_env(),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if self._server_process.stdout is not None:
            threading.Thread(
                target=self._consume_pipe,
                args=(self._server_process.stdout, self._server_stdout, True),
                daemon=True,
            ).start()
        if self._server_process.stderr is not None:
            threading.Thread(
                target=self._consume_pipe,
                args=(self._server_process.stderr, self._server_stderr, True),
                daemon=True,
            ).start()

    def _ensure_server(self) -> str:
        with self._start_lock:
            if self._server_process and self._server_process.poll() is not None:
                self._server_process = None
                self._server_url = None
                self._session_id = None

            if not self._server_process:
                self._log_progress(
                    f"[opencode] mode=serve model={self.model} agent={self.agent} starting server"
                )
                self._start_server_process()

            deadline = time.time() + min(self.timeout, 30)
            while time.time() < deadline:
                if self._server_process and self._server_process.poll() is not None:
                    raise RuntimeError(
                        self._error_context("serve", f"OpenCode serve exited with code {self._server_process.poll()}")
                    )
                if self._server_url:
                    try:
                        self._http_json("GET", "/global/health")
                        self._log_progress(
                            f"[opencode] mode=serve model={self.model} agent={self.agent} url={self._server_url}"
                        )
                        return self._server_url
                    except RuntimeError:
                        time.sleep(0.1)
                else:
                    time.sleep(0.1)
        raise RuntimeError(self._error_context("serve", "OpenCode serve did not become ready"))

    def _http_json(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        if not self._server_url:
            raise RuntimeError("OpenCode server URL is not available")
        data = None
        headers = self._request_headers({"Content-Type": "application/json"})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        request = Request(f"{self._server_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=min(self.timeout, 30)) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenCode HTTP {method} {path} failed: {exc.code} {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"OpenCode HTTP {method} {path} failed: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"text": raw}

    def _session_from_response(self, response: Any) -> Optional[str]:
        if isinstance(response, dict):
            for key in ("id", "sessionID", "sessionId"):
                value = response.get(key)
                if value:
                    return str(value)
            session = response.get("session")
            if isinstance(session, dict):
                return self._session_from_response(session)
        return None

    def _ensure_session(self) -> str:
        if self._session_id:
            try:
                query = urlencode({"directory": self.root_dir})
                self._http_json("GET", f"/session/{quote(self._session_id)}?{query}")
                return self._session_id
            except RuntimeError as exc:
                message = str(exc).lower()
                if "404" not in message and "not found" not in message:
                    raise
                self._log_progress(f"[opencode] session stale; creating a new session: {self._session_id}")
                self._session_id = None
        query = urlencode({"directory": self.root_dir})
        response = self._http_json("POST", f"/session?{query}", {})
        session_id = self._session_from_response(response)
        if not session_id:
            raise RuntimeError(self._error_context("serve", f"OpenCode session response had no id: {response!r}"))
        self._session_id = session_id
        self._log_progress(f"[opencode] session started: {session_id}")
        return session_id

    def _model_ref(self) -> Dict[str, str]:
        provider_id, _, model_id = self.model.partition("/")
        if not provider_id or not model_id:
            raise RuntimeError(f"Invalid OpenCode model '{self.model}'. Expected provider/model.")
        return {"providerID": provider_id, "modelID": model_id}

    def _prompt_payload(self, prompt: str) -> Dict[str, Any]:
        return {
            "parts": [{"type": "text", "text": prompt}],
            "model": self._model_ref(),
            "agent": self.agent,
            "tools": DENIED_OPENCODE_TOOLS,
        }

    def _request_headers(self, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        result = dict(headers or {})
        if self.server_password:
            token = base64.b64encode(f"{self.server_username}:{self.server_password}".encode("utf-8")).decode("ascii")
            result["Authorization"] = f"Basic {token}"
        return result

    def _handle_sse_event(self, event: Dict[str, Any], session_id: str) -> bool:
        event_session_id = self._event_session_id(event)
        if event_session_id and event_session_id != session_id:
            return False

        event_type = event.get("type")
        properties = self._event_properties(event)
        if event_type == "server.heartbeat":
            return False
        if event_type == "session.error":
            raise RuntimeError(self._error_message(properties))
        if event_type == "session.status":
            status = properties.get("status")
            session = properties.get("session")
            if status is None and isinstance(session, dict):
                status = session.get("status")
            if isinstance(status, dict):
                status = status.get("type")
            return status == "idle"
        if event_type == "message.part.delta":
            part_id = properties.get("partID") or properties.get("partId") or properties.get("id")
            delta = properties.get("delta")
            if part_id and isinstance(delta, str):
                text = self._text_parts.get(str(part_id), "") + delta
                self._text_parts[str(part_id)] = text
                self._last_text = text
            return False
        if event_type == "message.part.updated":
            part = properties.get("part")
            if isinstance(part, dict):
                part_type = part.get("type")
                if part_type == "text" and part.get("text") is not None:
                    text = str(part["text"])
                    part_id = part.get("id")
                    if part_id:
                        self._text_parts[str(part_id)] = text
                    self._last_text = text
                elif part_type == "tool":
                    self._log_progress(self._tool_progress_text({"part": part}))
                elif part_type == "step-finish":
                    self.last_step_finish = part
                    self._log_progress(self._step_finish_progress_text(part))
            return False
        if event_type == "step_finish":
            self.last_step_finish = properties
            self._log_progress(self._step_finish_progress_text(properties))
        if event_type == "tool_use":
            self._log_progress(self._tool_progress_text(properties))
        return False

    def _read_sse_until_idle(self, session_id: str, started_at_ms: int) -> str:
        if not self._server_url:
            raise RuntimeError("OpenCode server URL is not available")
        query = urlencode({"directory": self.root_dir})
        request = Request(
            f"{self._server_url}/event?{query}",
            headers=self._request_headers({"Accept": "text/event-stream"}),
            method="GET",
        )
        deadline = time.time() + self.timeout
        data_lines: List[str] = []
        try:
            with urlopen(request, timeout=min(self.timeout, 30)) as response:
                while time.time() < deadline:
                    raw = response.readline()
                    if raw == b"":
                        break
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                        continue
                    if line:
                        continue
                    if not data_lines:
                        continue
                    payload = "\n".join(data_lines)
                    data_lines = []
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    self._recent_sse_events.append(payload)
                    del self._recent_sse_events[:-10]
                    if self._handle_sse_event(event, session_id):
                        output = self._last_text.strip()
                        if output:
                            self._emit_usage_delta(output)
                            return output
                        fallback = self._fallback_text_from_db(started_at_ms)
                        if fallback:
                            self._emit_usage_delta(fallback)
                            self._emit_stream_delta(fallback)
                            return fallback
                        raise RuntimeError(self._error_context("serve", "OpenCode reached idle with no text output"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenCode SSE failed: {exc.code} {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"OpenCode SSE failed: {exc}") from exc
        except (OSError, TimeoutError) as exc:
            self._abort_session(session_id)
            raise RuntimeError(self._error_context("serve", f"OpenCode timed out after {self.timeout:g}s")) from exc

        self._abort_session(session_id)
        raise RuntimeError(self._error_context("serve", f"OpenCode timed out after {self.timeout:g}s"))

    def _abort_session(self, session_id: str) -> None:
        try:
            self._http_json("POST", f"/session/{quote(session_id)}/abort", {})
        except Exception as exc:
            self._log_progress(f"[opencode] abort failed: {exc}")

    def _generate_serve_once(self, prompt: str) -> str:
        started_at_ms = int(time.time() * 1000)
        self._last_text = ""
        self._text_parts = {}
        self._recent_sse_events = []
        self._ensure_server()
        session_id = self._ensure_session()
        query = urlencode({"directory": self.root_dir})
        path = f"/session/{quote(session_id)}/prompt_async?{query}"
        self._http_json("POST", path, self._prompt_payload(prompt))
        return self._read_sse_until_idle(session_id, started_at_ms)

    def _generate_serve(self, prompt: str) -> str:
        try:
            return self._generate_serve_once(prompt)
        except RuntimeError as exc:
            message = str(exc).lower()
            if "not found" not in message and "404" not in message:
                raise
            self._session_id = None
            return self._generate_serve_once(prompt)

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

    def _generate_run(self, prompt: str) -> str:
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

    def generate(self, prompt: str) -> str:
        if not self._serve_enabled():
            return self._generate_run(prompt)

        try:
            return self._generate_serve(prompt)
        except Exception as serve_exc:
            if not self._run_fallback_enabled():
                raise RuntimeError(self._error_context("serve", f"OpenCode serve failed: {serve_exc}")) from serve_exc
            self._log_progress(f"[opencode] serve failed; falling back to run: {serve_exc}")
            try:
                return self._generate_run(prompt)
            except Exception as run_exc:
                raise RuntimeError(
                    f"{self._error_context('serve', f'OpenCode serve failed: {serve_exc}')}\n"
                    f"Run fallback also failed:\n{run_exc}"
                ) from run_exc
