#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import urlopen

from llm.factory import ProviderFactory
from terminal_colors import strip_leading_bracket_name



PRINT_LOCK = threading.Lock()
SKILL_SEARCH_DIRS = ("Playbooks/claude-vibe", "Playbooks", "playbooks/claude-vibe", "playbooks")
SKILL_FILE_NAMES = ("SKILL.md", "README.md", "README.txt", "skill.md", "instructions.md")
SKILL_CATALOG_PATHS = ("Playbooks/skill-catalog.json", "playbooks/skill-catalog.json", ".matted_skill_catalog.json")
SKILL_CACHE_DIR = "Playbooks/.cache"
DEFAULT_SKILL_AUTO_DOWNLOAD = "1"
DEFAULT_SKILL_WEB_SEARCH = "1"
DEFAULT_SKILL_WEB_AUTO_DOWNLOAD = "0"
DEFAULT_SKILL_WEB_ALLOWED_HOSTS = "raw.githubusercontent.com,github.com"
DEFAULT_SKILL_DOWNLOAD_TIMEOUT_S = 15
MAX_SKILL_CHARS = 24000
DEFAULT_ROUTER_CONTEXT_MODE = "adaptive"


def safe_print(message: str) -> None:
    with PRINT_LOCK:
        print(f"\r{strip_leading_bracket_name(message)}", flush=True)


def utc_ts_ms() -> int:
    return int(time.time() * 1000)


def db_connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    ensure_master_tratada_column(con)
    return con


def ensure_master_tratada_column(con: sqlite3.Connection) -> None:
    columns = [row[1] for row in con.execute("PRAGMA table_info(tarefas)").fetchall()]
    if columns and "master_tratada" not in columns:
        safe_print("[master] Migrating tarefas: adding master_tratada column")
        con.execute(
            "ALTER TABLE tarefas ADD COLUMN master_tratada INTEGER NOT NULL DEFAULT 0 CHECK (master_tratada IN (0,1))"
        )
    if columns:
        mark_previously_handled_tasks(con)


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
        safe_print(f"[master] Migrating tarefas: marked {cur.rowcount} previously handled task(s)")


class MasterOrchestrator:
    def __init__(
        self,
        *,
        root: Path,
        agents: list[str],
        poll_interval_s: float = 0.5,
        tmux_session: str | None = None,
    ) -> None:
        self.root_dir = root.resolve()
        self.root = self.root_dir
        self.db_path = self.root_dir / "squad.db"
        self.poll_interval_s = poll_interval_s
        self.agents = agents
        self.tmux_session = tmux_session
        self.project_dir = Path(__file__).resolve().parent
        self.agent_script = self.project_dir / "agent_base.py"
        self.print_lock = PRINT_LOCK
        self.stop_event = threading.Event()
        self.input_active = False
        self.alerted_approval_task_ids: set[int] = set()
        # Initialize the LLM provider via the factory
        self.llm_provider = ProviderFactory.get_provider(root_dir=str(self.root_dir))

        # Ensure tmux server is running and session exists
        if self.tmux_session:
            self._ensure_tmux_session()



    def _ensure_tmux_session(self) -> None:
        """Ensures that the tmux server is running and the target session exists."""
        try:
            # Try to list sessions to check if server is running
            subprocess.run(["tmux", "ls"], capture_output=True, check=False)
        except FileNotFoundError:
            self._log("[master] ERROR: tmux is not installed. Please install it: sudo apt update && sudo apt install tmux")
            return

        # Create session if it doesn't exist
        subprocess.run(["tmux", "new-session", "-d", "-s", self.tmux_session], capture_output=True, check=False)
        self._configure_tmux_pane_labels(self.tmux_session)

    def _configure_tmux_pane_labels(self, session: str) -> None:
        """Configura labels fixas nas bordas dos panes para identificar agentes."""
        commands = [
            ["tmux", "set-option", "-t", session, "-g", "pane-border-status", "bottom"],
            [
                "tmux",
                "set-option",
                "-t",
                session,
                "-g",
                "pane-border-format",
                "#[align=right] #{?@agent_label,#{@agent_label},#[bold]#T#[default]} ",
            ],
        ]
        for cmd in commands:
            subprocess.run(cmd, capture_output=True, text=True, check=False)

    def _agent_pane_style(self, agent_name: str) -> str:
        palette = (
            "colour39",
            "colour81",
            "colour118",
            "colour178",
            "colour208",
            "colour171",
            "colour45",
        )
        idx = sum(ord(ch) for ch in agent_name) % len(palette)
        return palette[idx]

    def _label_agent_pane(self, session: str, pane_id: str, agent_name: str) -> None:
        color = self._agent_pane_style(agent_name)
        label = f"#[fg={color},bold]{agent_name}#[default]"
        subprocess.run(["tmux", "select-pane", "-t", pane_id, "-T", label], capture_output=True, text=True, check=False)
        subprocess.run(["tmux", "set-option", "-pt", pane_id, "pane-border-style", f"fg={color}"], capture_output=True, text=True, check=False)
        subprocess.run(
            ["tmux", "set-option", "-pt", pane_id, "pane-active-border-style", f"fg={color},bold"],
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(["tmux", "set-option", "-pt", pane_id, "window-style", "default"], capture_output=True, text=True, check=False)
        subprocess.run(["tmux", "set-option", "-pt", pane_id, "window-active-style", "default"], capture_output=True, text=True, check=False)

    def focar_agente(self, nome_agente: str) -> bool:
        """
        Focuses the tmux pane associated with the given agent name.
        Returns True if successful, False otherwise.
        """
        try:
            # 1. Find PID of the agent process
            pid_cmd = ["pgrep", "-f", f"--name {nome_agente}"]
            pid_result = subprocess.run(pid_cmd, capture_output=True, text=True, check=False)
            pid_out = pid_result.stdout.strip()

            if not pid_out:
                self._log(f"[master] Could not find PID for agent '{nome_agente}'")
                return False

            # If multiple PIDs are returned, take the first one
            pid = pid_out.split('\n')[0]

            # 2. Find the Pane ID associated with that PID
            pane_cmd = ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"]
            pane_result = subprocess.run(pane_cmd, capture_output=True, text=True, check=True)

            pane_id = None
            for line in pane_result.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] == pid:
                    pane_id = parts[0]
                    break

            if not pane_id:
                self._log(f"[master] Could not find tmux pane for PID {pid} (agent '{nome_agente}')")
                return False

            # 3. Select the pane
            subprocess.run(["tmux", "select-pane", "-t", pane_id], check=True)
            self._log(f"[master] Focused pane for agent '{nome_agente}' (Pane ID: {pane_id})")
            return True

        except Exception as e:
            self._log(f"[master] Error focusing agent '{nome_agente}': {e}")
            return False

    def _agent_process_pids(self, agent_name: str) -> set[str]:
        result = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False)
        pids: set[str] = set()
        needle = f"--name {agent_name}"
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            pid, _, args = stripped.partition(" ")
            if "agent_base.py" in args and needle in args:
                pids.add(pid)
        return pids

    def _process_descendants(self, root_pid: str) -> set[str]:
        result = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, check=False)
        children_by_parent: dict[str, list[str]] = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) != 2:
                continue
            pid, ppid = parts
            children_by_parent.setdefault(ppid, []).append(pid)

        descendants: set[str] = set()
        stack = list(children_by_parent.get(root_pid, []))
        while stack:
            pid = stack.pop()
            if pid in descendants:
                continue
            descendants.add(pid)
            stack.extend(children_by_parent.get(pid, []))
        return descendants

    def _agent_pane_ids(self, agent_name: str) -> set[str]:
        panes = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}\t#{pane_title}"],
            capture_output=True,
            text=True,
            check=False,
        )
        agent_pids = self._agent_process_pids(agent_name)
        pane_ids: set[str] = set()
        for line in panes.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            pane_id, pane_pid, pane_title = parts
            if agent_name in pane_title:
                pane_ids.add(pane_id)
                continue
            pane_processes = {pane_pid} | self._process_descendants(pane_pid)
            if pane_processes & agent_pids:
                pane_ids.add(pane_id)
        return pane_ids

    def encerrar_agente(self, nome_agente: str) -> bool:
        sanitized = self._sanitize_agent_name(nome_agente)
        if not sanitized:
            return False
        try:
            pane_ids = self._agent_pane_ids(sanitized)
            for pane_id in pane_ids:
                subprocess.run(["tmux", "kill-pane", "-t", pane_id], capture_output=True, text=True, check=False)

            # If the pane lookup failed, still terminate matching worker processes.
            for pid in self._agent_process_pids(sanitized):
                subprocess.run(["kill", pid], capture_output=True, text=True, check=False)

            if sanitized in self.agents:
                self.agents.remove(sanitized)

            self._log(f"[master] Encerrado agente '{sanitized}' ({len(pane_ids)} pane(s))")
            return bool(pane_ids) or sanitized not in self.agents
        except Exception as e:
            self._log(f"[master] Error encerrando agente '{sanitized}': {e}")
            return False

    def _agent_process_running(self, nome_agente: str) -> bool:
        try:
            result = subprocess.run(
                ["pgrep", "-f", f"--name {nome_agente}"],
                capture_output=True,
                text=True,
                check=False,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    def _log(self, message: str) -> None:
        with self.print_lock:
            print(f"\r{strip_leading_bracket_name(message)}", flush=True)
            if self.input_active:
                print("Você: ", end="", flush=True)

    def _resolve_tmux_session(self) -> str:
        if self.tmux_session:
            self._configure_tmux_pane_labels(self.tmux_session)
            return self.tmux_session

        if os.environ.get("TMUX"):
            result = subprocess.run(
                ["tmux", "display-message", "-p", "#S"],
                capture_output=True,
                text=True,
                check=True,
            )
            session = result.stdout.strip()
            if session:
                self.tmux_session = session
                self._configure_tmux_pane_labels(session)
                self._log(f"[master] Detected active tmux session: {session}")
                return session

        session = os.environ.get("SESSION", "ai_squad")
        subprocess.run(["tmux", "has-session", "-t", session], capture_output=True, text=True, check=True)
        self.tmux_session = session
        self._configure_tmux_pane_labels(session)
        self._log(f"[master] Using configured tmux session: {session}")
        return session

    def criar_novo_agente(self, nome: str, skill_prompt: str, *, resolve_agent_name_as_skill: bool = True) -> None:
        session = self._resolve_tmux_session()
        root_dir = str(self.root_dir)
        agent_skill_ref = nome if resolve_agent_name_as_skill else None
        resolved_skill_prompt, loaded_skills = self._augment_skill_prompt(skill_prompt, agent_name=agent_skill_ref)

        # NEW: Install any tools described in the skill prompt
        installed_tools = self._install_skill_tools(resolved_skill_prompt)
        if installed_tools:
            tools_text = "\n".join([f"- {t}" for t in installed_tools])
            resolved_skill_prompt += f"\n\nSYSTEM TOOLS INSTALLED:\n{tools_text}\nUse these commands in your terminal to leverage the installed skill tools."

        if loaded_skills:
            self._log(f"[master] Loaded skills for agent '{nome}': {', '.join(loaded_skills)}")
        else:
            self._log(f"[master] No project skill loaded for agent '{nome}'")
        # Write system prompt to a temp file to avoid hitting OS arg-length limits
        # (long skills + task descriptions can exceed the tmux command size limit).
        prompt_fd, prompt_path = tempfile.mkstemp(prefix=f"matted_prompt_{nome}_", suffix=".txt", dir=str(self.root_dir))
        os.close(prompt_fd)
        prompt_file = Path(prompt_path)
        prompt_file.write_text(resolved_skill_prompt, encoding="utf-8")

        command = shlex.join(
            [
                "python3",
                "-u",
                str(self.agent_script),
                "--root",
                root_dir,
                "--name",
                nome,
                "--system-prompt-file",
                str(prompt_file),
            ]
        )
        self._log(f"[master] Spawning dynamic agent '{nome}' in tmux session '{session}'")
        split_result = subprocess.run(
            [
                "tmux",
                "split-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                f"{session}:",
                "-c",
                root_dir,
                command,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            ["tmux", "select-layout", "-t", f"{session}:", "main-vertical"],
            capture_output=True,
            text=True,
            check=True,
        )
        pane_id = split_result.stdout.strip()
        if pane_id:
            self._label_agent_pane(session, pane_id, nome)
        self._log(f"[master] Dynamic agent '{nome}' spawned successfully")

    def _build_specialist_decision_prompt(self, task: sqlite3.Row, projeto: dict[str, Any], historico: list[dict[str, Any]]) -> str:
        return (
            "Voce e o Master de um orquestrador multiagente local.\n"
            "Avalie se a proxima etapa precisa de um especialista novo fora da fila atual.\n"
            "Responda somente JSON valido, sem markdown, no formato:\n"
            '{"spawn": false, "nome": "", "skill_prompt": "", "solicitacao": ""}\n\n'
            "Regras:\n"
            "- Use spawn=true apenas se uma skill especifica for claramente necessaria.\n"
            "- nome deve ser curto, em snake_case, sem espacos.\n"
            "- skill_prompt deve explicar a especialidade e responsabilidades do agente.\n"
            "- solicitacao deve ser a tarefa concreta para esse novo agente.\n\n"
            f"AGENTES_ATUAIS:\n{json.dumps(self.agents, ensure_ascii=True)}\n\n"
            f"PROJETO:\n{json.dumps(projeto, ensure_ascii=True)}\n\n"
            f"HISTORICO_RECENTE:\n{json.dumps(historico, ensure_ascii=True)}\n\n"
            f"TAREFA_CONCLUIDA:\n{json.dumps(dict(task), ensure_ascii=True)}\n"
        )

    def _extract_json_object(self, text: str) -> dict[str, Any] | None:
        text = text.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None

    def _sanitize_agent_name(self, name: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", name.strip().lower()).strip("_")
        return cleaned[:48]

    def _extract_skill_refs(self, text: str) -> list[str]:
        # Recompose common hard-wrap artifacts from terminal/pane output
        # like "codebase-a\nnalyzer" -> "codebase-analyzer".
        normalized_text = text.replace("\xa0", " ")
        normalized_text = re.sub(r"([A-Za-z0-9_.-])\s*\n\s*([A-Za-z0-9_.-])", r"\1\2", normalized_text)
        refs: list[str] = []

        def add_ref(raw_ref: str) -> None:
            ref = raw_ref.strip("`'\".,;: )]")
            lowered = ref.lower()
            if not ref or len(ref) < 2:
                return
            if lowered in {"http", "https", "file", "a", "o", "e", "de", "da", "do", "no", "na"}:
                return
            if ref not in refs:
                refs.append(ref)

        url_pattern = r"(?:https?|file)://[^\s`'\"),\]]+"
        for match in re.finditer(url_pattern, normalized_text, flags=re.IGNORECASE):
            add_ref(match.group(0))

        text_without_urls = re.sub(url_pattern, " ", normalized_text, flags=re.IGNORECASE)
        patterns = (
            r"skill\s+(?:dele\s+)?(?:sera|será|é|eh|=|:)\s*[`'\"]?([A-Za-z0-9_.-]+)",
            r"skill\s+references?\s*[:=]\s*[`'\"]?([A-Za-z0-9_.-]+)",
            r"skill\s*[:=]\s*[`'\"]?([A-Za-z0-9_.-]+)",
            r"skill\s+([A-Za-z0-9_.-]+)\s*(?:existente|em|no|na|do|da)",
            r"\b([A-Za-z0-9_.-]+)\s*\(existente\s+no\s+Playbooks/claude-vibe\)",
        )
        for pattern in patterns:
            for match in re.finditer(pattern, text_without_urls, flags=re.IGNORECASE):
                add_ref(match.group(1))
        return refs

    def _skill_ref_variants(self, skill_ref: str) -> list[str]:
        cleaned = skill_ref.strip().strip("`'\".,;: /\\")
        if not cleaned:
            return []
        variants = [cleaned]
        swapped_hyphen = cleaned.replace("_", "-")
        swapped_underscore = cleaned.replace("-", "_")
        for variant in (swapped_hyphen, swapped_underscore):
            if variant and variant not in variants:
                variants.append(variant)
        return variants

    def _candidate_skill_paths(self, skill_ref: str) -> list[Path]:
        safe_ref = skill_ref.strip().strip("/\\")
        if not safe_ref or ".." in Path(safe_ref).parts:
            return []

        candidates: list[Path] = []
        raw = Path(safe_ref)
        bases = [self.root_dir / search_dir for search_dir in SKILL_SEARCH_DIRS]

        if raw.parts and raw.parts[0].lower() == "playbooks":
            candidates.append(self.root_dir / raw)
        else:
            candidates.append(self.root_dir / raw)
            for base in bases:
                candidates.append(base / safe_ref)

        expanded: list[Path] = []
        for candidate in candidates:
            expanded.append(candidate)
            if candidate.suffix == "":
                for suffix in (".md", ".txt"):
                    expanded.append(candidate.with_suffix(suffix))

        unique: list[Path] = []
        seen: set[Path] = set()
        for candidate in expanded:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(self.root_dir)
            except (OSError, ValueError):
                continue
            if resolved not in seen:
                seen.add(resolved)
                unique.append(resolved)
        return unique

    def _read_skill_path(self, path: Path) -> tuple[Path, str] | None:
        try:
            if path.is_file():
                return path, path.read_text(encoding="utf-8", errors="replace")
            if path.is_dir():
                parts: list[str] = []
                for file_name in SKILL_FILE_NAMES:
                    file_path = path / file_name
                    if file_path.is_file():
                        parts.append(file_path.read_text(encoding="utf-8", errors="replace"))
                if not parts:
                    for file_path in sorted(path.glob("*.md"))[:5]:
                        if file_path.is_file():
                            parts.append(file_path.read_text(encoding="utf-8", errors="replace"))
                if parts:
                    return path, "\n\n".join(parts)
        except OSError as e:
            self._log(f"[master] Failed reading skill path '{path}': {e}")
        return None

    def _load_skill_reference(self, skill_ref: str) -> tuple[Path, str] | None:
        for variant in self._skill_ref_variants(skill_ref):
            parsed = urlparse(variant)
            registry_first = (
                parsed.scheme in {"https", "http"}
                and parsed.netloc.lower() not in {"github.com", "raw.githubusercontent.com"}
                and not parsed.path.lower().endswith((".md", ".txt"))
            )
            if registry_first:
                registry = self._fetch_skill_from_registry_url(variant)
                if registry:
                    return registry
            direct = self._download_direct_skill_reference(variant)
            if direct:
                return direct
            if not registry_first:
                # Try registry-style fetch for non-GitHub URLs (npx, npm, etc.)
                registry = self._fetch_skill_from_registry_url(variant)
                if registry:
                    return registry
            for candidate in self._candidate_skill_paths(variant):
                loaded = self._read_skill_path(candidate)
                if loaded:
                    return loaded
            downloaded = self._download_catalog_skill(variant)
            if downloaded:
                return downloaded
            web_loaded = self._search_and_optionally_download_skill(variant)
            if web_loaded:
                return web_loaded
        return None

    def _download_direct_skill_reference(self, skill_ref: str) -> tuple[Path, str] | None:
        parsed = urlparse(skill_ref)
        if parsed.scheme not in {"https", "http", "file"}:
            return None
        if parsed.scheme in {"https", "http"} and not self._is_allowed_skill_candidate_url(parsed):
            self._log(f"[master] Direct skill URL blocked by host allowlist: {skill_ref}")
            return None
        if not self._skill_auto_download_enabled():
            self._log(f"[master] Direct skill URL provided, but MATTED_SKILL_AUTO_DOWNLOAD is off")
            return None

        safe_name = self._sanitize_agent_name(Path(parsed.path).stem or "remote_skill")
        cache_dir = self.root_dir / SKILL_CACHE_DIR / safe_name

        # If user passed a GitHub repository URL, clone the full repo so all
        # files (SKILL.md, scripts, configs, templates) are available to agents.
        # Fall back to git pull if directory already exists.
        if parsed.scheme in {"https", "http"} and parsed.netloc.lower() == "github.com":
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2:
                owner, repo = parts[0], parts[1]
                repo_clean = repo[:-4] if repo.endswith(".git") else repo

                # Detect GitHub sub-path: /tree/<branch>/<subdir> or /blob/<branch>/<path>
                # e.g. /mamamou/ai-coding-skills/tree/main/code-reviewer → subdir=code-reviewer
                skill_subdir: str | None = None
                skill_branch: str | None = None
                if len(parts) >= 5 and parts[2] in ("tree", "blob"):
                    skill_branch = parts[3]
                    skill_subdir = "/".join(parts[4:])

                clone_url = f"https://github.com/{owner}/{repo_clean}.git"

                self._log(f"[master] Cloning skill repository {clone_url} into {cache_dir.relative_to(self.root_dir)}...")
                if os.path.isdir(os.path.join(cache_dir, ".git")):
                    # Already cloned — try to update
                    remotes_check = subprocess.run(
                        ["git", "remote"], cwd=str(cache_dir), capture_output=True, text=True
                    )
                    if remotes_check.stdout.strip():
                        try:
                            subprocess.run(
                                ["git", "pull"], cwd=str(cache_dir),
                                capture_output=True, text=True, check=True
                            )
                            self._log(f"[master] Updated existing skill repo '{cache_dir.name}': Already up to date.")
                        except subprocess.CalledProcessError as pull_err:
                            stderr_msg = pull_err.stderr.strip()
                            if "no tracking information" in stderr_msg.lower() or "no remote" in stderr_msg.lower():
                                self._log(f"[master] No remote configured for '{cache_dir.name}'. Keeping as-is.")
                            else:
                                self._log(f"[master] git pull failed for '{cache_dir.name}': {stderr_msg}. Keeping existing files.")
                    else:
                        self._log(f"[master] No remote configured for '{cache_dir.name}'. Keeping as-is.")
                else:
                    # Fresh clone
                    if cache_dir.exists() and any(cache_dir.iterdir()):
                        self._log(f"[master] Skill cache dir already exists without .git: {cache_dir.relative_to(self.root_dir)}. Keeping existing files.")
                    else:
                        cache_dir.mkdir(parents=True, exist_ok=True)
                        try:
                            clone_cmd = ["git", "clone", clone_url, str(cache_dir)]
                            if skill_branch:
                                clone_cmd[2:2] = ["--branch", skill_branch]
                            subprocess.run(
                                clone_cmd,
                                capture_output=True, text=True, check=True
                            )
                            self._log(f"[master] Successfully cloned {clone_url} -> {cache_dir.relative_to(self.root_dir)}")
                        except subprocess.CalledProcessError as clone_err:
                            self._log(f"[master] Failed to clone '{clone_url}': {clone_err.stderr.strip()}")
                            # Fall back to raw file download below

                # Look for SKILL.md inside the cloned repo.
                # If user specified a sub-path (e.g. /tree/main/code-reviewer),
                # look inside that subdir so the agent gets the right skill.
                skill_dir: Path | None
                if skill_subdir:
                    skill_dir = cache_dir / skill_subdir
                    if not skill_dir.is_dir():
                        self._log(f"[master] Sub-path '{skill_subdir}' not found in cloned repo. Trying raw fallback.")
                        skill_dir = None
                    else:
                        self._log(f"[master] Using sub-path '{skill_subdir}' for skill content.")
                else:
                    skill_dir = cache_dir

                loaded = self._read_skill_path(skill_dir) if skill_dir else None
                if loaded and skill_dir:
                    self._log(f"[master] Loaded skill from cloned repo: {skill_dir.relative_to(self.root_dir)}")
                    return loaded
                if skill_dir:
                    self._log(f"[master] Cloned repo has no SKILL.md/README.md at expected path. Listing available files...")
                    for f in sorted(cache_dir.rglob("*.md"))[:10]:
                        self._log(f"  - {f.relative_to(cache_dir)}")
                # Fall back to raw download below

        # Fallback: try raw GitHub file URLs (direct SKILL.md / README.md)
        candidate_urls = [skill_ref]
        if parsed.scheme in {"https", "http"} and parsed.netloc.lower() == "github.com":
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2:
                owner, repo = parts[0], parts[1]
                repo_clean = repo[:-4] if repo.endswith(".git") else repo
                candidate_urls = []
                if len(parts) >= 5 and parts[2] in ("tree", "blob"):
                    branch = parts[3]
                    subpath = "/".join(parts[4:])
                    if parts[2] == "blob":
                        candidate_urls.append(f"https://raw.githubusercontent.com/{owner}/{repo_clean}/{branch}/{subpath}")
                    else:
                        candidate_urls.extend(
                            [
                                f"https://raw.githubusercontent.com/{owner}/{repo_clean}/{branch}/{subpath}/SKILL.md",
                                f"https://raw.githubusercontent.com/{owner}/{repo_clean}/{branch}/{subpath}/skill.md",
                                f"https://raw.githubusercontent.com/{owner}/{repo_clean}/{branch}/{subpath}/README.md",
                            ]
                        )
                candidate_urls.extend([
                    f"https://raw.githubusercontent.com/{owner}/{repo_clean}/main/SKILL.md",
                    f"https://raw.githubusercontent.com/{owner}/{repo_clean}/master/SKILL.md",
                    f"https://raw.githubusercontent.com/{owner}/{repo_clean}/main/skill.md",
                    f"https://raw.githubusercontent.com/{owner}/{repo_clean}/master/skill.md",
                    f"https://raw.githubusercontent.com/{owner}/{repo_clean}/main/README.md",
                    f"https://raw.githubusercontent.com/{owner}/{repo_clean}/master/README.md",
                ])

        last_error: Exception | None = None
        downloaded_url = ""
        for url in candidate_urls:
            try:
                self._download_skill_file(url, cache_dir / "SKILL.md")
                downloaded_url = url
                last_error = None
                break
            except Exception as e:
                last_error = e
                continue
        if last_error is not None:
            self._log(f"[master] Failed downloading direct skill URL '{skill_ref}': {last_error}")
            return None

        self._coerce_downloaded_markdown_to_skill(cache_dir / "SKILL.md", source_url=downloaded_url, skill_ref=skill_ref)

        loaded = self._read_skill_path(cache_dir)
        if loaded:
            self._log(f"[master] Downloaded direct skill URL into {cache_dir.relative_to(self.root_dir)}")
        return loaded

    def _fetch_skill_from_registry_url(self, skill_ref: str) -> tuple[Path, str] | None:
        """
        Fetches a skill from a non-GitHub registry/website URL by:
        1. Fetching the page content (HTML or markdown).
        2. Searching for installation commands (npx, npm, git clone, etc.).
        3. Executing found commands to install the skill into cache.
        4. Looking for SKILL.md-like content on the page itself as fallback.
        """
        parsed = urlparse(skill_ref)
        if parsed.scheme not in {"https", "http"}:
            return None
        if not self._is_allowed_skill_candidate_url(parsed):
            return None
        if not self._skill_auto_download_enabled():
            return None

        self._log(f"[master] Attempting registry-style skill fetch from: {skill_ref}")
        safe_name = self._sanitize_agent_name(Path(parsed.path).stem or "registry_skill")
        cache_dir = self.root_dir / SKILL_CACHE_DIR / safe_name
        cache_dir.mkdir(parents=True, exist_ok=True)

        # 1. Fetch the page content
        try:
            page_content = self._fetch_url_text(skill_ref)
        except Exception as e:
            self._log(f"[master] Failed to fetch registry URL '{skill_ref}': {e}")
            return None

        # 2. Strip HTML tags to get clean text for parsing
        clean_text = re.sub(r"<script[^>]*>.*?</script>", " ", page_content, flags=re.DOTALL | re.IGNORECASE)
        clean_text = re.sub(r"<style[^>]*>.*?</style>", " ", clean_text, flags=re.DOTALL | re.IGNORECASE)
        clean_text = re.sub(r"<[^>]+>", " ", clean_text)
        clean_text = html.unescape(clean_text)

        # 3. Look for installation commands in code blocks and inline text
        # Ordered from most-specific to least-specific so dedup keeps the longer match
        install_patterns = [
            # npx playbooks add skill owner/repo --skill name
            r"npx\s+[\w-]+\s+add\s+skill\s+[^\s]+\s*(?:--skill\s+\S+)?",
            # npx @scope/pkg add skill ...
            r"npx\s+@[\w-]+/[\w-]+\s+add\s+skill\s+[^\s]+\s*(?:--skill\s+\S+)?",
            # git clone https://... with optional target dir
            r"git\s+clone\s+https?://\S+(?:\s+\S+)?",
            # npm install -g package
            r"npm\s+(?:install|i)\s+(?:-g\s+)?\S+",
            # curl | sh / bash installers
            r"curl\s+[^\s|]+(?:\s*\|\s*(?:bash|sh))?",
            # pip install -r or package (but not standalone -r)
            r"pip(?:3)?\s+install\s+(?!-r\s*$|--requirement\s*$)\S+",
        ]

        found_commands: list[str] = []
        # First check fenced code blocks
        code_blocks = re.findall(r"```(?:bash|shell|sh|text|plain|cmd|terminal)?\n(.*?)\n```", clean_text, re.DOTALL | re.IGNORECASE)

        search_text = "\n".join(code_blocks) if code_blocks else clean_text

        # Deduplicate: longer commands that contain shorter ones win
        raw_matches: list[tuple[str, int]] = []
        for pattern in install_patterns:
            for match in re.finditer(pattern, search_text, re.IGNORECASE):
                cmd = match.group(0).strip()
                # Normalize backslash continuations
                cmd = re.sub(r"\\\s*\n", " ", cmd)
                # Trim trailing punctuation that's not part of the command
                cmd = cmd.rstrip(".,;:`'\" ")
                if cmd:
                    raw_matches.append((cmd, match.start()))

        # Sort by position, then deduplicate (skip a match if it's a substring of an earlier longer match at nearby position)
        raw_matches.sort(key=lambda x: x[1])
        seen_positions: list[tuple[int, int]] = []  # (start, end) in search_text
        for cmd, pos in raw_matches:
            # Check if this match is fully contained within an already-seeright match
            match_end = pos + len(cmd)
            contained = False
            for s, e in seen_positions:
                if s <= pos and match_end <= e:
                    contained = True
                    break
            if not contained:
                found_commands.append(cmd)
                seen_positions.append((pos, match_end))

        # 4. Execute found install commands
        if found_commands:
            cwd = str(cache_dir)
            for cmd in found_commands:
                self._log(f"[master] Registry skill install command: {cmd}")
                parts = [p.strip() for p in cmd.split("&&")]
                for part in parts:
                    if not part:
                        continue
                    # Track cd
                    cd_match = re.match(r"^cd\s+(.+)$", part)
                    if cd_match:
                        target = cd_match.group(1).strip().strip('"').strip("'")
                        cwd = os.path.abspath(os.path.join(cwd, target))
                        if not os.path.isdir(cwd):
                            os.makedirs(cwd, exist_ok=True)
                        continue
                    # Handle git clone (check existing dir)
                    git_clone_match = re.search(r"\bgit\s+clone\s+(\S+)(?:\s+(\S+))?", part, re.IGNORECASE)
                    if git_clone_match:
                        repo_url = git_clone_match.group(1)
                        target_dir = git_clone_match.group(2)
                        if not target_dir:
                            from urllib.parse import urlparse as _up
                            pu = _up(repo_url)
                            path_parts = [p for p in pu.path.split("/") if p]
                            if path_parts:
                                last = path_parts[-1]
                                target_dir = last[:-4] if last.endswith(".git") else last
                        if target_dir:
                            resolved = os.path.abspath(os.path.join(cwd, target_dir))
                            if os.path.isdir(os.path.join(resolved, ".git")):
                                remotes = subprocess.run(["git", "remote"], cwd=resolved, capture_output=True, text=True)
                                if remotes.stdout.strip():
                                    try:
                                        subprocess.run(["git", "pull"], cwd=resolved, capture_output=True, text=True, check=True)
                                        self._log(f"[master] Updated existing repo: {resolved}")
                                    except subprocess.CalledProcessError:
                                        self._log(f"[master] git pull failed for '{resolved}', keeping existing.")
                                cwd = resolved
                                continue
                            if os.path.isdir(resolved):
                                self._log(f"[master] Existing non-git directory for clone target '{resolved}'. Keeping existing files.")
                                cwd = resolved
                                continue
                    try:
                        subprocess.run(part, shell=True, check=True, capture_output=True, text=True, cwd=cwd)
                        self._log(f"[master] Installed registry skill tool: {part}")
                    except subprocess.CalledProcessError as e:
                        self._log(f"[master] Install command failed '{part}': {e.stderr.strip()}")
                        break  # stop chain on failure

            # 5. After install, look for SKILL.md inside cache
            loaded = self._read_skill_path(cache_dir)
            if loaded:
                self._log(f"[master] Skill loaded from registry install at {cache_dir.relative_to(self.root_dir)}")
                return loaded

        # 6. Fallback: try to extract skill-like content from the page itself
        #    (some registry pages embed the SKILL.md directly or describe setup inline)
        skill_content = self._try_extract_skill_from_page(clean_text, skill_ref)
        if skill_content:
            skill_file = cache_dir / "SKILL.md"
            skill_file.write_text(skill_content, encoding="utf-8")
            self._log(f"[master] Skill content extracted from registry page: {cache_dir.relative_to(self.root_dir)}")
            return cache_dir, skill_content

        self._log(f"[master] Registry URL yielded no installable skill content: {skill_ref}")
        return None

    def _try_extract_skill_from_page(self, page_text: str, source_url: str) -> str | None:
        """
        Attempts to extract skill-like structured content from a registry page.
        Looks for a heading that identifies the skill, then captures the doc body.
        Returns markdown text suitable as SKILL.md, or None if nothing usable found.
        """
        # Remove nav/common boilerplate lines
        lines = page_text.splitlines()
        content_lines: list[str] = []
        in_content = False
        heading_found = False

        for line in lines:
            stripped = line.strip()
            # Skip empty, nav, and footer patterns
            if not stripped:
                if in_content:
                    content_lines.append("")
                continue
            if len(stripped) < 3:
                continue

            # Detect a heading that looks like a skill title
            if re.match(r"^(#{1,3}\s+)?[A-Z][A-Za-z0-9 _-]{3,80}$", stripped) and not heading_found:
                heading_found = True
                in_content = True
                content_lines.append(f"# {stripped.lstrip('#').strip()}")
                continue

            if in_content:
                # Stop at common footer markers
                if re.match(r"^(footer|copyright|©|all rights|powered by|made with)", stripped, re.IGNORECASE):
                    break
                content_lines.append(line)

        if heading_found and len(content_lines) >= 3:
            body = "\n".join(content_lines).strip()
            if len(body) > 100:  # minimum usable content length
                return (
                    f"SKILL DERIVED FROM REGISTRY PAGE\n"
                    f"Source: {source_url}\n"
                    f"Use this documentation as operational guidance for the agent.\n\n"
                    f"{body}\n"
                )
        return None

    def _coerce_downloaded_markdown_to_skill(self, destination: Path, *, source_url: str, skill_ref: str) -> None:
        lower_url = source_url.lower()
        if "readme.md" not in lower_url:
            return
        try:
            body = destination.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as e:
            self._log(f"[master] Failed to normalize downloaded README skill '{skill_ref}': {e}")
            return

        wrapped = (
            "SKILL DERIVED FROM REPOSITORY README\n"
            f"Source: {skill_ref}\n"
            "Use this repository documentation as operational guidance for the agent. "
            "Prefer extracting workflow, constraints, setup requirements, supported inputs, outputs, and limitations. "
            "Do not assume the repository is already installed locally unless the task explicitly requires cloning or setup.\n\n"
            f"{body}\n"
        )
        try:
            destination.write_text(wrapped, encoding="utf-8")
        except OSError as e:
            self._log(f"[master] Failed to write normalized README skill '{skill_ref}': {e}")

    def _skill_auto_download_enabled(self) -> bool:
        raw = os.environ.get("MATTED_SKILL_AUTO_DOWNLOAD", DEFAULT_SKILL_AUTO_DOWNLOAD)
        return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}

    def _load_skill_catalog(self) -> dict[str, Any]:
        catalog: dict[str, Any] = {}
        paths = [self.root_dir / path for path in SKILL_CATALOG_PATHS]
        env_path = os.environ.get("MATTED_SKILL_CATALOG")
        if env_path:
            paths.insert(0, Path(env_path) if Path(env_path).is_absolute() else self.root_dir / env_path)

        for path in paths:
            try:
                resolved = path.resolve()
                resolved.relative_to(self.root_dir)
            except (OSError, ValueError):
                continue
            if not resolved.is_file():
                continue
            try:
                data = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                self._log(f"[master] Skill catalog ignored '{resolved}': {e}")
                continue
            catalog.update(self._normalize_skill_catalog(data))
        return catalog

    def _normalize_skill_catalog(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}

        skills = data.get("skills", data)
        aliases = data.get("aliases", {})
        normalized: dict[str, Any] = {}
        if isinstance(skills, dict):
            for name, entry in skills.items():
                key = self._normalize_skill_key(str(name))
                if isinstance(entry, str):
                    normalized[key] = {"url": entry}
                elif isinstance(entry, dict):
                    normalized[key] = dict(entry)

        if isinstance(aliases, dict):
            for alias, target in aliases.items():
                target_key = self._normalize_skill_key(str(target))
                if target_key in normalized:
                    normalized[self._normalize_skill_key(str(alias))] = normalized[target_key]
        return normalized

    def _normalize_skill_key(self, value: str) -> str:
        return value.strip().strip("/\\").lower()

    def _catalog_keys_for_skill(self, skill_ref: str) -> list[str]:
        keys: list[str] = []
        for variant in self._skill_ref_variants(skill_ref):
            normalized = self._normalize_skill_key(variant)
            if normalized not in keys:
                keys.append(normalized)
            if "/" not in normalized:
                for prefixed in (f"claude-vibe/{normalized}", f"playbooks/claude-vibe/{normalized}"):
                    if prefixed not in keys:
                        keys.append(prefixed)
        return keys

    def _catalog_entry_for_skill(self, skill_ref: str) -> dict[str, Any] | None:
        catalog = self._load_skill_catalog()
        for key in self._catalog_keys_for_skill(skill_ref):
            entry = catalog.get(key)
            if isinstance(entry, dict):
                return entry
        return None

    def _download_catalog_skill(self, skill_ref: str) -> tuple[Path, str] | None:
        entry = self._catalog_entry_for_skill(skill_ref)
        if not entry:
            self._log(f"[master] Skill reference '{skill_ref}' not found locally or in known skill catalog")
            return None
        if not self._skill_auto_download_enabled():
            self._log(f"[master] Skill '{skill_ref}' found in catalog, but MATTED_SKILL_AUTO_DOWNLOAD is off")
            return None

        cache_dir = self.root_dir / SKILL_CACHE_DIR / self._sanitize_agent_name(skill_ref)
        try:
            cache_dir.resolve().relative_to(self.root_dir)
        except (OSError, ValueError):
            return None

        try:
            self._download_skill_entry(entry, cache_dir)
        except Exception as e:
            self._log(f"[master] Failed downloading skill '{skill_ref}' from catalog: {e}")
            return None

        loaded = self._read_skill_path(cache_dir)
        if loaded:
            self._log(f"[master] Downloaded skill '{skill_ref}' into {cache_dir.relative_to(self.root_dir)}")
        return loaded

    def _download_skill_entry(self, entry: dict[str, Any], cache_dir: Path) -> None:
        files = entry.get("files")
        if isinstance(files, list):
            for item in files:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "")
                rel_path = str(item.get("path") or "SKILL.md")
                checksum = str(item.get("sha256") or "")
                self._download_skill_file(url, cache_dir / rel_path, checksum or None)
            return

        url = str(entry.get("url") or "")
        if not url:
            raise ValueError("catalog entry has no url")
        rel_path = str(entry.get("path") or "SKILL.md")
        checksum = str(entry.get("sha256") or "")
        self._download_skill_file(url, cache_dir / rel_path, checksum or None)

    def _download_skill_file(self, url: str, destination: Path, sha256: str | None = None) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"https", "http", "file"}:
            raise ValueError(f"unsupported skill URL scheme: {parsed.scheme}")

        destination = destination.resolve()
        destination.relative_to(self.root_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)

        timeout = float(os.environ.get("MATTED_SKILL_DOWNLOAD_TIMEOUT", DEFAULT_SKILL_DOWNLOAD_TIMEOUT_S))
        with urlopen(url, timeout=timeout) as response:
            content = response.read()

        if sha256:
            actual = hashlib.sha256(content).hexdigest()
            if actual.lower() != sha256.lower():
                raise ValueError(f"sha256 mismatch for {url}")

        destination.write_bytes(content)

    def _skill_web_search_enabled(self) -> bool:
        raw = os.environ.get("MATTED_SKILL_WEB_SEARCH", DEFAULT_SKILL_WEB_SEARCH)
        return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}

    def _skill_web_auto_download_enabled(self) -> bool:
        raw = os.environ.get("MATTED_SKILL_WEB_AUTO_DOWNLOAD", DEFAULT_SKILL_WEB_AUTO_DOWNLOAD)
        return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}

    def _allowed_skill_web_hosts(self) -> set[str]:
        raw = os.environ.get("MATTED_SKILL_WEB_ALLOWED_HOSTS", DEFAULT_SKILL_WEB_ALLOWED_HOSTS)
        return {part.strip().lower() for part in raw.split(",") if part.strip()}

    def _search_and_optionally_download_skill(self, skill_ref: str) -> tuple[Path, str] | None:
        if not self._skill_web_search_enabled():
            return None

        candidates = self._web_search_skill_candidates(skill_ref)
        if not candidates:
            self._log(f"[master] Web skill search found no candidates for '{skill_ref}'")
            return None

        candidates_path = self._write_skill_web_candidates(skill_ref, candidates)
        self._log(f"[master] Web skill search found {len(candidates)} candidate(s) for '{skill_ref}': {candidates_path}")

        if not self._skill_web_auto_download_enabled():
            self._log(
                f"[master] Web auto-download is off. Review candidates and set MATTED_SKILL_WEB_AUTO_DOWNLOAD=1 to allow it."
            )
            return None

        cache_dir = self.root_dir / SKILL_CACHE_DIR / self._sanitize_agent_name(skill_ref)
        try:
            self._download_skill_file(candidates[0]["url"], cache_dir / "SKILL.md")
        except Exception as e:
            self._log(f"[master] Failed downloading web skill candidate for '{skill_ref}': {e}")
            return None

        loaded = self._read_skill_path(cache_dir)
        if loaded:
            self._log(f"[master] Downloaded web skill '{skill_ref}' into {cache_dir.relative_to(self.root_dir)}")
        return loaded

    def _write_skill_web_candidates(self, skill_ref: str, candidates: list[dict[str, str]]) -> Path:
        safe_name = self._sanitize_agent_name(skill_ref) or "skill"
        path = self.root_dir / SKILL_CACHE_DIR / ".web-candidates" / f"{safe_name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(candidates, indent=2, ensure_ascii=False), encoding="utf-8")
        return path.relative_to(self.root_dir)

    def _web_search_skill_candidates(self, skill_ref: str) -> list[dict[str, str]]:
        query = quote_plus(f'{skill_ref} "SKILL.md" OR "README.md" github skill')
        url = f"https://duckduckgo.com/html/?q={query}"
        try:
            page = self._fetch_url_text(url)
        except Exception as e:
            self._log(f"[master] Web skill search failed for '{skill_ref}': {e}")
            return []

        candidates: list[dict[str, str]] = []
        seen: set[str] = set()
        for result_url in self._extract_search_result_urls(page):
            normalized_url = self._normalize_skill_candidate_url(result_url)
            if not normalized_url or normalized_url in seen:
                continue
            seen.add(normalized_url)
            candidates.append({"url": normalized_url, "source": result_url})
            if len(candidates) >= 5:
                break
        return candidates

    def _fetch_url_text(self, url: str) -> str:
        timeout = float(os.environ.get("MATTED_SKILL_DOWNLOAD_TIMEOUT", DEFAULT_SKILL_DOWNLOAD_TIMEOUT_S))
        with urlopen(url, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")

    def _extract_search_result_urls(self, page_html: str) -> list[str]:
        urls: list[str] = []
        for raw_href in re.findall(r'href=["\']([^"\']+)["\']', page_html, flags=re.IGNORECASE):
            href = html.unescape(raw_href)
            parsed = urlparse(href)
            if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
                target = parse_qs(parsed.query).get("uddg", [""])[0]
                href = unquote(target) if target else href
            elif href.startswith("/l/"):
                target = parse_qs(urlparse(href).query).get("uddg", [""])[0]
                href = unquote(target) if target else href
            if href.startswith("http://") or href.startswith("https://") or href.startswith("file://"):
                urls.append(href)
        return urls

    def _normalize_skill_candidate_url(self, url: str) -> str | None:
        parsed = urlparse(url)
        if not self._is_allowed_skill_candidate_url(parsed):
            return None

        if parsed.scheme == "file":
            return url

        host = parsed.netloc.lower()
        path = parsed.path
        if host == "github.com" and "/blob/" in path:
            parts = path.strip("/").split("/")
            if len(parts) >= 5 and parts[2] == "blob":
                owner, repo, branch = parts[0], parts[1], parts[3]
                file_path = "/".join(parts[4:])
                return f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"

        if host == "raw.githubusercontent.com":
            lowered = path.lower()
            if lowered.endswith((".md", ".txt")) or "skill.md" in lowered or "readme.md" in lowered:
                return url
        return None

    def _is_allowed_skill_candidate_url(self, parsed) -> bool:
        if parsed.scheme == "file":
            return os.environ.get("MATTED_ALLOW_FILE_SKILL_URLS", "0").strip().lower() in {"1", "true", "yes", "sim", "on"}
        if parsed.scheme not in {"https", "http"}:
            return False
        host = parsed.netloc.lower()
        allowed = self._allowed_skill_web_hosts()
        return any(host == allowed_host or host.endswith(f".{allowed_host}") for allowed_host in allowed)

    def _install_skill_tools(self, skill_content: str) -> list[str]:
        """
        Scans skill content for installation commands (git clone, npx, etc.)
        and executes them to make tools available to agents.
        Returns a list of installed tool commands.
        """
        installed_commands = []
        # Look for bash/shell code blocks
        code_blocks = re.findall(r"```(?:bash|shell|sh)\n(.*?)\n```", skill_content, re.DOTALL | re.IGNORECASE)

        # Fallback: look for common install patterns even without blocks
        if not code_blocks:
            # Simple regex for common install patterns
            patterns = [
                r"git\s+clone\s+https?://\S+",
                r"npx\s+[\w/-]+\s+add\s+[\w/-]+",
                r"npm\s+install\s+-g\s+[\w/-]+",
                r"pip\s+install\s+[\w/-]+"
            ]
            for p in patterns:
                matches = re.findall(p, skill_content, re.IGNORECASE)
                for m in matches:
                    code_blocks.append(m)

        for block in code_blocks:
            # Normalize backslash line continuations
            block = re.sub(r"\\\s*\n", " ", block)
            lines = block.splitlines()
            cwd = str(self.root_dir)
            for line in lines:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                # Basic safety check on the full line first
                if not any(cmd in line.lower() for cmd in ("git clone", "npx", "npm install", "pip install", "cd ")):
                    continue

                # Process the line (handling potential chained commands with &&)
                parts = [p.strip() for p in line.split("&&")]
                for part in parts:
                    if not part:
                        continue

                    # Handle cd
                    cd_match = re.match(r"^cd\s+(.+)$", part)
                    if cd_match:
                        target = cd_match.group(1).strip().strip('"').strip("'")
                        if target == "~":
                            cwd = os.path.expanduser("~")
                        else:
                            cwd = os.path.abspath(os.path.join(cwd, target))
                        if not os.path.isdir(cwd):
                            self._log(f"[master] Directory does not exist for cd: {cwd}")
                        continue

                    # Check if this subcommand is allowed
                    if not any(cmd in part.lower() for cmd in ("git clone", "npx", "npm install", "pip install")):
                        self._log(f"[master] Skipping unsafe or non-whitelisted subcommand: {part}")
                        continue

                    # Handle git clone
                    git_clone_match = re.search(r"\bgit\s+clone\s+(\S+)(?:\s+(\S+))?", part, re.IGNORECASE)
                    if git_clone_match:
                        repo_url = git_clone_match.group(1)
                        target_dir = git_clone_match.group(2)
                        if not target_dir:
                            try:
                                parsed_url = urlparse(repo_url)
                                path_parts = [p for p in parsed_url.path.split('/') if p]
                                if path_parts:
                                    last_part = path_parts[-1]
                                    if last_part.endswith(".git"):
                                        last_part = last_part[:-4]
                                    target_dir = last_part
                            except Exception:
                                target_dir = ""

                        if target_dir:
                            resolved_target = os.path.abspath(os.path.join(cwd, target_dir))
                            if os.path.isdir(os.path.join(resolved_target, ".git")):
                                self._log(f"[master] Directory '{resolved_target}' already exists. Attempting git pull...")
                                # Check if repo has a remote configured
                                remotes_check = subprocess.run(
                                    ["git", "remote"],
                                    cwd=resolved_target,
                                    capture_output=True,
                                    text=True
                                )
                                if not remotes_check.stdout.strip():
                                    self._log(f"[master] No remote configured for '{resolved_target}'. Skipping update — repo already present.")
                                    installed_commands.append(f"git clone {repo_url} {target_dir} (already exists)")
                                    cwd = resolved_target
                                    continue
                                try:
                                    pull_res = subprocess.run(
                                        ["git", "pull"],
                                        cwd=resolved_target,
                                        capture_output=True,
                                        text=True,
                                        check=True
                                    )
                                    self._log(f"[master] Updated existing repo in '{resolved_target}': {pull_res.stdout.strip()}")
                                    installed_commands.append(f"git -C {target_dir} pull")
                                    # Set cwd to target_dir so subsequent actions (e.g. npm install) can run in it if we didn't have cd
                                    cwd = resolved_target
                                    continue
                                except subprocess.CalledProcessError as pull_err:
                                    stderr_msg = pull_err.stderr.strip()
                                    self._log(f"[master] Failed to git pull in '{resolved_target}': {stderr_msg}")
                                    # Don't break the chain for non-fatal pulls — the repo exists so tools are usable
                                    if "no tracking information" in stderr_msg.lower() or "no remote" in stderr_msg.lower():
                                        self._log(f"[master] No tracking/remote configured. Keeping existing repo as-is.")
                                        installed_commands.append(f"git clone {repo_url} {target_dir} (already exists, no remote to pull)")
                                        cwd = resolved_target
                                        continue
                                    break # Stop executing this chain for other errors
                            if os.path.isdir(resolved_target):
                                self._log(f"[master] Directory '{resolved_target}' already exists without .git. Keeping existing files.")
                                installed_commands.append(f"git clone {repo_url} {target_dir} (already exists, not a git repo)")
                                cwd = resolved_target
                                continue

                    # Handle pip install
                    if "pip install" in part.lower():
                        try:
                            pip_parts = shlex.split(part)
                        except ValueError:
                            pip_parts = part.split()

                        has_incomplete_r = False
                        for i, p_arg in enumerate(pip_parts):
                            if p_arg in ("-r", "--requirement"):
                                if i + 1 >= len(pip_parts) or pip_parts[i+1].startswith("-"):
                                    has_incomplete_r = True
                                    # Try to reconstruct
                                    requirements_file = os.path.join(cwd, "requirements.txt")
                                    if os.path.isfile(requirements_file):
                                        pip_parts.insert(i+1, "requirements.txt")
                                        part = shlex.join(pip_parts)
                                        self._log(f"[master] Reconstructed pip install command with requirements.txt: {part}")
                                        has_incomplete_r = False
                                    break
                        if has_incomplete_r:
                            self._log(f"[master] Ignoring incomplete pip install command: {part}")
                            continue

                    # Execute the subcommand
                    try:
                        self._log(f"[master] Installing skill tool: {part} (cwd: {cwd})")
                        subprocess.run(part, shell=True, check=True, capture_output=True, text=True, cwd=cwd)
                        installed_commands.append(part)
                    except subprocess.CalledProcessError as e:
                        self._log(f"[master] Failed to install tool '{part}': {e.stderr.strip()}")
                        break # Stop executing this chain

        return installed_commands

    def _augment_skill_prompt(self, skill_prompt: str, agent_name: str | None = None) -> tuple[str, list[str]]:
        loaded_blocks: list[str] = []
        loaded_labels: list[str] = []
        skill_refs = self._extract_skill_refs(skill_prompt)
        if agent_name and not skill_refs:
            if agent_name not in skill_refs:
                skill_refs.append(agent_name)

        for skill_ref in skill_refs:
            loaded = self._load_skill_reference(skill_ref)
            if not loaded:
                self._log(f"[master] Skill reference '{skill_ref}' is not available locally or in catalog")
                continue

            path, content = loaded
            relative = path.relative_to(self.root_dir)
            loaded_labels.append(str(relative))
            loaded_blocks.append(
                "SKILL CARREGADA DO PROJETO\n"
                f"Referencia: {skill_ref}\n"
                f"Caminho: {relative}\n"
                "Conteudo:\n"
                f"{content[:MAX_SKILL_CHARS]}"
            )

        if not loaded_blocks:
            return skill_prompt, []

        augmented = (
            f"{skill_prompt.rstrip()}\n\n"
            "INSTRUCOES DE SKILLS CARREGADAS:\n"
            "As instrucoes abaixo foram lidas do projeto pelo Master. "
            "Use-as como autoridade principal para operar este agente.\n\n"
            + "\n\n---\n\n".join(loaded_blocks)
        )
        return augmented, loaded_labels

    def _fetch_routing_context(self, con: sqlite3.Connection) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        proj = con.execute(
            "SELECT id, status_global, tecnologias, ultima_atualizacao FROM projeto WHERE id=1"
        ).fetchone()
        projeto = dict(proj) if proj else {}
        hist = con.execute(
            "SELECT id, autor, mensagem, timestamp FROM historico ORDER BY timestamp DESC, id DESC LIMIT 20"
        ).fetchall()
        historico = [dict(r) for r in reversed(hist)]
        return projeto, historico

    def _generate_planner_output(self, prompt: str) -> str:
        previous_stream = os.environ.get("MATTED_OPENROUTER_STREAM")
        os.environ["MATTED_OPENROUTER_STREAM"] = "0"
        try:
            return self.llm_provider.generate(prompt)
        finally:
            if previous_stream is None:
                os.environ.pop("MATTED_OPENROUTER_STREAM", None)
            else:
                os.environ["MATTED_OPENROUTER_STREAM"] = previous_stream

    def _avaliar_necessidade_de_especialista(
        self,
        con: sqlite3.Connection,
        task: sqlite3.Row,
    ) -> dict[str, str] | None:
        projeto, historico = self._fetch_routing_context(con)
        prompt = self._build_specialist_decision_prompt(task, projeto, historico)
        self._log(f"[master] Asking LLM if task id={task['id']} needs a dynamic specialist")
        try:
            output = self._generate_planner_output(prompt)
        except Exception as e:
            self._log(f"[master] Specialist decision skipped: {e}")
            return None



        decision = self._extract_json_object(output)
        if not decision or not bool(decision.get("spawn")):
            self._log(f"[master] Specialist decision for task id={task['id']}: no dynamic agent needed")
            return None

        nome = self._sanitize_agent_name(str(decision.get("nome") or ""))
        skill_prompt = str(decision.get("skill_prompt") or "").strip()
        solicitacao = str(decision.get("solicitacao") or "").strip()
        if not nome or not skill_prompt or not solicitacao:
            self._log(f"[master] Specialist decision ignored: incomplete Codex response {decision!r}")
            return None
        if nome in self.agents:
            self._log(f"[master] Specialist decision ignored: agent '{nome}' already exists in route")
            return None

        return {"nome": nome, "skill_prompt": skill_prompt, "solicitacao": solicitacao}

    def seed(self) -> None:
        if not self.db_path.exists():
            self._log(f"[master] Seed requested but DB missing: {self.db_path}")
            return
        con = db_connect(self.db_path)
        try:
            total_tasks = int(con.execute("SELECT COUNT(*) FROM tarefas").fetchone()[0])
            projeto = con.execute("SELECT status_global FROM projeto WHERE id=1").fetchone()
            status_global = projeto["status_global"] if projeto else None
            if total_tasks > 0 or status_global != "Planejamento":
                self._log(
                    "[master] Seed skipped: workflow already started "
                    f"(tarefas={total_tasks}, status_global={status_global!r})"
                )
                return
            if not self.agents:
                self._log("[master] Seed skipped: no agents configured in Dynamic Swarm mode")
                return

            now = utc_ts_ms()
            first_agent = self.agents[0]
            con.execute(
                "INSERT INTO tarefas (agente_destino, status, solicitacao, resposta, data_criacao) VALUES (?,?,?,?,?)",
                (
                    first_agent,
                    "pendente",
                    f"Executar a etapa inicial do projeto como agente '{first_agent}' conforme o contexto atual (tabela projeto + historico).",
                    None,
                    now,
                ),
            )
            con.execute(
                "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                ("master", f"Seed: criei a primeira tarefa para {first_agent}.", now),
            )
            self._log(f"[master] Seed inserted: tarefa pendente para {first_agent}")
        finally:
            con.close()

    def _enqueue_task(self, con: sqlite3.Connection, *, agent: str, solicitacao: str) -> int:
        now = utc_ts_ms()
        cur = con.execute(
            "INSERT INTO tarefas (agente_destino, status, solicitacao, resposta, data_criacao) VALUES (?,?,?,?,?)",
            (agent, "pendente", solicitacao, None, now),
        )
        task_id = int(cur.lastrowid)
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            ("master", f"Enfileirei tarefa #{task_id} para {agent}: {solicitacao}", now),
        )
        self._log(f"[master] Enqueued task id={task_id} -> {agent} (pendente)")
        return task_id

    def _update_project(self, con: sqlite3.Connection, *, status_global: str | None = None, tecnologias: str | None = None) -> None:
        now = utc_ts_ms()
        row = con.execute("SELECT status_global, tecnologias FROM projeto WHERE id=1").fetchone()
        cur_status = row["status_global"] if row else "Planejamento"
        cur_tech = row["tecnologias"] if row else "[]"

        new_status = status_global if status_global is not None else cur_status
        new_tech = tecnologias if tecnologias is not None else cur_tech
        con.execute(
            "UPDATE projeto SET status_global=?, tecnologias=?, ultima_atualizacao=? WHERE id=1",
            (new_status, new_tech, now),
        )

    def _handle_completed_task(
        self,
        con: sqlite3.Connection,
        task: sqlite3.Row,
        continuation_decision: dict[str, Any] | None = None,
    ) -> None:
        task_id = int(task["id"])
        agente = task["agente_destino"]

        self._log(f"[master] Handling concluded task id={task_id} from_agent={agente}")
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            ("master", f"Recebi conclusao da tarefa #{task_id} ({agente}).", utc_ts_ms()),
        )

        self._update_project(con, status_global=f"{agente}_concluido")

        if continuation_decision and continuation_decision.get("continue"):
            next_agent = str(continuation_decision["agent"])
            next_task_id = self._enqueue_task(con, agent=next_agent, solicitacao=str(continuation_decision["solicitacao"]))
            motivo = str(continuation_decision.get("motivo") or "continuidade automatica")
            con.execute(
                "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                (
                    "master",
                    f"Agente '{agente}' concluiu tarefa #{task_id}. Encaminhei continuidade para '{next_agent}' "
                    f"na tarefa #{next_task_id}. Motivo: {motivo}",
                    utc_ts_ms(),
                ),
            )
            self._log(f"[master] Agent '{agente}' completed task id={task_id}; routed next task #{next_task_id} -> {next_agent}")
        else:
            motivo = "sem decisao automatica"
            if continuation_decision:
                motivo = str(continuation_decision.get("motivo") or motivo)
            con.execute(
                "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                ("master", f"Agente '{agente}' concluiu tarefa #{task_id}. Sem proxima tarefa automatica: {motivo}", utc_ts_ms()),
            )
            self._log(f"[master] Agent '{agente}' completed task id={task_id}; no automatic next task")

        con.execute("UPDATE tarefas SET master_tratada=1 WHERE id=?", (task_id,))
        self._log(f"[master] Marked task id={task_id} as master_tratada=1")

    def _build_user_task_prompt(self, user_prompt: str, projeto: dict[str, Any], historico: list[dict[str, Any]]) -> str:
        return (
            "Voce e o Master de um Dynamic Swarm local.\n"
            "Sua funcao e orquestrar agentes de forma real e funcional.\n"
            "Analise a mensagem do usuario e decida se deve criar novo agente, focar agente existente ou apenas responder.\n"
            "Responda sempre somente JSON valido, sem markdown, no formato:\n"
            '{"spawn": true, "nome_agente": "backend_login", "skill_prompt": "Voce e especialista em backend...", "tarefa_inicial": "Implemente...", "resposta_usuario": "Vou criar um agente backend para cuidar disso.", "focar_agente": "", "encerrar_agentes": []}\n\n'
            "Regras:\n"
            "- resposta_usuario e obrigatoria e deve conter a mensagem conversacional que sera exibida ao usuario.\n"
            "- Use spawn=true quando o usuario pedir para criar/chamar um agente ou quando a solicitacao exigir execucao por especialista.\n"
            "- Use focar_agente quando o usuario quiser ver, abrir ou focar em um agente que ja existe (ex: 'me mostre o agente X', 'abra o pane do Y'). Preencha focar_agente com o nome do agente e use spawn=false.\n"
            "- Use encerrar_agentes como lista de agentes existentes quando o usuario pedir para encerrar, matar, substituir ou recriar agentes em loop.\n"
            "- Use spawn=false se a mensagem for apenas conversa, status, pergunta simples, ou nao exigir novo agente.\n"
            "- Se o usuario disser apenas ola/oi, responda cordialmente em resposta_usuario, use spawn=false e aguarde novo comando.\n"
            "- Se precisar de mais contexto/permissao antes de criar tarefa, pergunte em resposta_usuario, use spawn=false e aguarde.\n"
            "- nome_agente deve ser curto, em snake_case, sem espacos, acentos ou simbolos.\n"
            "- skill_prompt deve descrever a especialidade, responsabilidades e estilo de trabalho do agente.\n"
            "- tarefa_inicial deve ser a primeira tarefa concreta a inserir na fila do novo agente.\n"
            "- Se spawn=false e focar_agente e vazio, deixe nome_agente, skill_prompt e tarefa_inicial como strings vazias, mas preencha resposta_usuario.\n"
            "- Nao inclua explicacoes fora do JSON.\n\n"
            f"AGENTES_EXISTENTES:\n{json.dumps(self.agents, ensure_ascii=True)}\n\n"
            f"PROJETO:\n{json.dumps(projeto, ensure_ascii=True)}\n\n"
            f"HISTORICO_RECENTE:\n{json.dumps(historico, ensure_ascii=True)}\n\n"
            f"SOLICITACAO_DO_USUARIO:\n{user_prompt}\n"
        )

    def _compact_history_for_router(self, historico: list[dict[str, Any]], max_items: int = 8, max_msg_chars: int = 280) -> list[dict[str, Any]]:
        compact: list[dict[str, Any]] = []
        for item in historico[-max_items:]:
            msg = str(item.get("mensagem") or "")
            compact.append(
                {
                    "id": item.get("id"),
                    "autor": item.get("autor"),
                    "timestamp": item.get("timestamp"),
                    "mensagem": msg[:max_msg_chars],
                }
            )
        return compact

    def _router_context_mode(self) -> str:
        raw = os.environ.get("MATTED_ROUTER_CONTEXT_MODE", DEFAULT_ROUTER_CONTEXT_MODE).strip().lower()
        if raw in {"compact", "normal", "adaptive"}:
            return raw
        return DEFAULT_ROUTER_CONTEXT_MODE

    def _is_complex_user_prompt(self, user_prompt: str) -> bool:
        lowered = user_prompt.strip().lower()
        if len(lowered) > 260:
            return True
        complexity_tokens = (
            "refator",
            "arquitet",
            "migr",
            "security",
            "vulner",
            "teste",
            "ci",
            "pipeline",
            "deploy",
            "api",
            "banco",
            "schema",
            "performance",
            "otimiz",
            "debug",
            "erro",
        )
        return any(token in lowered for token in complexity_tokens)

    def _history_for_router(self, user_prompt: str, historico: list[dict[str, Any]]) -> list[dict[str, Any]]:
        mode = self._router_context_mode()
        if mode == "normal":
            return historico
        if mode == "compact":
            return self._compact_history_for_router(historico)
        if self._is_complex_user_prompt(user_prompt):
            return historico
        return self._compact_history_for_router(historico)

    def _simple_user_response(self, user_prompt: str) -> str | None:
        normalized = user_prompt.strip().lower()
        if normalized in {"oi", "ola", "olá", "bom dia", "boa tarde", "boa noite"}:
            return "Olá! Estou pronto para orquestrar o swarm. Diga a próxima ação."
        if normalized in {"status", "estado", "como está o projeto?", "como esta o projeto?"}:
            return "Posso te passar o status atual e quais agentes estão ativos. Se quiser, eu consulto isso agora."
        return None

    def _is_pause_command(self, user_prompt: str) -> bool:
        normalized = user_prompt.strip().lower()
        return normalized in {"pausar", "pausar execucao", "pausar execução", "pause", "pause global"}

    def _is_resume_command(self, user_prompt: str) -> bool:
        normalized = user_prompt.strip().lower()
        return normalized in {"retomar", "retomar execucao", "retomar execução", "continuar", "resume", "unpause"}

    def _agent_names_to_end_from_text(self, text: str) -> list[str]:
        lowered = text.lower()
        if not any(token in lowered for token in ("encerrar", "matar", "kill", "fechar", "substituir", "recriar")):
            return []
        matches: list[str] = []
        for agent_name in self.agents:
            if agent_name in text and agent_name not in matches:
                matches.append(agent_name)
        return matches


    def _parse_swarm_decision(self, output: str) -> dict[str, Any] | None:
        parsed = self._extract_json_object(output)
        if not parsed:
            return None

        spawn = bool(parsed.get("spawn"))
        nome_agente = self._sanitize_agent_name(str(parsed.get("nome_agente") or ""))
        skill_prompt = str(parsed.get("skill_prompt") or "").strip()
        tarefa_inicial = str(parsed.get("tarefa_inicial") or "").strip()
        resposta_usuario = str(parsed.get("resposta_usuario") or "").strip()
        focar_agente = str(parsed.get("focar_agente") or "").strip()
        raw_encerrar = parsed.get("encerrar_agentes", [])
        encerrar_agentes: list[str] = []
        if isinstance(raw_encerrar, list):
            for item in raw_encerrar:
                agent_name = self._sanitize_agent_name(str(item or ""))
                if agent_name and agent_name not in encerrar_agentes:
                    encerrar_agentes.append(agent_name)

        if not resposta_usuario:
            return None

        if not spawn:
            return {
                "spawn": False,
                "nome_agente": "",
                "skill_prompt": "",
                "tarefa_inicial": "",
                "resposta_usuario": resposta_usuario,
                "focar_agente": focar_agente,
                "encerrar_agentes": encerrar_agentes,
            }
        if not nome_agente or not skill_prompt or not tarefa_inicial:
            return None
        return {
            "spawn": True,
            "nome_agente": nome_agente,
            "skill_prompt": skill_prompt,
            "tarefa_inicial": tarefa_inicial,
            "resposta_usuario": resposta_usuario,
            "focar_agente": focar_agente,
            "encerrar_agentes": encerrar_agentes,
        }

    def _build_continuation_prompt(self, task: sqlite3.Row, projeto: dict[str, Any], historico: list[dict[str, Any]]) -> str:
        return (
            "Voce e o Master de um Dynamic Swarm local.\n"
            "Um agente acabou de concluir uma tarefa. Decida imediatamente a proxima acao para manter o projeto andando.\n"
            "Responda somente JSON valido, sem markdown, no formato:\n"
            '{"continue": true, "agent": "backend_api", "spawn": false, "skill_prompt": "", "solicitacao": "Revise...", "motivo": "Proxima etapa logica"}\n\n'
            "Regras:\n"
            "- Prefira encaminhar a proxima tarefa para outro agente existente quando isso criar comunicacao util entre areas.\n"
            "- Use agentes de coordenacao como product_owner, po, scrum_master ou sm somente se eles existirem em AGENTES_EXISTENTES.\n"
            "- Se um agente necessario nao existir, use spawn=true, preencha agent e skill_prompt com uma especialidade completa.\n"
            "- Se spawn=false, agent deve ser um agente ja existente.\n"
            "- solicitacao deve pedir uma acao concreta e incluir o contexto que o agente anterior entregou.\n"
            "- Use continue=false somente se o projeto estiver realmente completo ou se for indispensavel aguardar o usuario humano.\n"
            "- Nao devolva tarefa para o mesmo agente quando outro agente puder revisar, orientar ou continuar.\n"
            "- Nao inclua explicacoes fora do JSON.\n\n"
            f"AGENTES_EXISTENTES:\n{json.dumps(self.agents, ensure_ascii=True)}\n\n"
            f"PROJETO:\n{json.dumps(projeto, ensure_ascii=True)}\n\n"
            f"HISTORICO_RECENTE:\n{json.dumps(historico, ensure_ascii=True)}\n\n"
            f"TAREFA_CONCLUIDA:\n{json.dumps(dict(task), ensure_ascii=True)}\n"
        )

    def _parse_continuation_decision(self, output: str) -> dict[str, Any] | None:
        parsed = self._extract_json_object(output)
        if not parsed:
            return None

        should_continue = bool(parsed.get("continue"))
        agent = self._sanitize_agent_name(str(parsed.get("agent") or ""))
        spawn = bool(parsed.get("spawn"))
        skill_prompt = str(parsed.get("skill_prompt") or "").strip()
        solicitacao = str(parsed.get("solicitacao") or "").strip()
        motivo = str(parsed.get("motivo") or "").strip()

        if not should_continue:
            return {
                "continue": False,
                "agent": "",
                "spawn": False,
                "skill_prompt": "",
                "solicitacao": "",
                "motivo": motivo or "Sem proxima tarefa automatica.",
            }

        if not agent or not solicitacao:
            return None
        if spawn and not skill_prompt:
            return None
        if not spawn and agent not in self.agents:
            return {
                "continue": False,
                "agent": "",
                "spawn": False,
                "skill_prompt": "",
                "solicitacao": "",
                "motivo": f"Planner escolheu agente inexistente '{agent}' com spawn=false.",
            }

        return {
            "continue": True,
            "agent": agent,
            "spawn": spawn,
            "skill_prompt": skill_prompt,
            "solicitacao": solicitacao,
            "motivo": motivo,
        }

    def _plan_continuation(self, con: sqlite3.Connection, task: sqlite3.Row) -> dict[str, Any] | None:
        projeto, historico = self._fetch_routing_context(con)
        prompt = self._build_continuation_prompt(task, projeto, historico)
        self._log(f"[master] Asking LLM to plan continuation for task id={task['id']}")
        try:
            output = self._generate_planner_output(prompt)
        except Exception as e:
            self._log(f"[master] Continuation planning failed: {e}")
            return None


        decision = self._parse_continuation_decision(output)
        if not decision:
            self._log("[master] Continuation planner returned invalid output; no automatic next task")
        return decision

    def _ensure_continuation_agent(self, decision: dict[str, Any] | None) -> bool:
        if not decision or not decision.get("continue"):
            return True

        agent_name = str(decision["agent"])
        if agent_name in self.agents:
            return True
        if not bool(decision.get("spawn")):
            self._log(f"[master] Continuation skipped: agent '{agent_name}' does not exist and spawn=false")
            return False

        try:
            self.criar_novo_agente(agent_name, str(decision["skill_prompt"]))
            self.agents.append(agent_name)
            self._log("[master] Waiting for continuation agent pane to initialize...")
            time.sleep(3)
            return True
        except subprocess.CalledProcessError as e:
            self._log(f"[master] Failed to spawn continuation agent '{agent_name}' with exit code {e.returncode}")
            if e.stderr:
                self._log(f"[master] tmux stderr: {e.stderr.strip()}")
            return False
        except FileNotFoundError:
            self._log("[master] Failed to spawn continuation agent: tmux not found in PATH")
            return False

    def restaurar_agentes_ativos(self) -> None:
        """Re-spawns all agents currently registered in the system to restore tmux panes."""
        if not self.agents:
            self._log("[master] No agents to restore.")
            return

        skill_prompts = self._load_agent_skill_prompts_from_history()
        self._log(f"[master] Restoring {len(self.agents)} active agents to tmux...")
        for agent_name in self.agents:
            if self._agent_process_running(agent_name):
                self._log(f"[master] Agent '{agent_name}' already running; skipping restore spawn")
                continue
            try:
                skill_prompt = skill_prompts.get(
                    agent_name,
                    f"You are the {agent_name} agent. Resume your activity from the database.",
                )
                self.criar_novo_agente(agent_name, skill_prompt, resolve_agent_name_as_skill=False)
                self._log(f"[master] Restored agent '{agent_name}'")
            except Exception as e:
                self._log(f"[master] Failed to restore agent '{agent_name}': {e}")

    def _load_agent_skill_prompts_from_history(self) -> dict[str, str]:
        if not self.db_path.exists():
            return {}
        con = db_connect(self.db_path)
        try:
            rows = con.execute(
                """
                SELECT mensagem
                FROM historico
                WHERE autor='master' AND mensagem LIKE 'Spawn/chat:%preparado com skill:%'
                ORDER BY id ASC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        finally:
            con.close()

        prompts: dict[str, str] = {}
        marker = " preparado com skill: "
        for row in rows:
            message = str(row["mensagem"] or "")
            match = re.search(r"Spawn/chat: agente '([^']+)' preparado com skill: ", message)
            if not match or marker not in message:
                continue
            agent_name = self._sanitize_agent_name(match.group(1))
            skill_prompt = message.split(marker, 1)[1].strip()
            if agent_name and skill_prompt:
                prompts[agent_name] = skill_prompt
        return prompts

    def _print_user_response(self, message: str) -> None:
        if not message.strip():
            return
        with self.print_lock:
            print("\nMaster:")
            print(message.strip())

    def _record_user_message(self, con: sqlite3.Connection, message: str) -> None:
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            ("usuario", message, utc_ts_ms()),
        )

    def _load_agents_from_db(self, con: sqlite3.Connection) -> list[str]:
        rows = con.execute(
            """
            SELECT agente_destino, MAX(id) AS max_id
            FROM tarefas
            GROUP BY agente_destino
            ORDER BY max_id ASC
            """
        ).fetchall()
        agents: list[str] = []
        for row in rows:
            name = self._sanitize_agent_name(str(row["agente_destino"] or ""))
            if name and name not in agents:
                agents.append(name)
        lifecycle = self._agent_lifecycle_from_history(con)
        return [name for name in agents if lifecycle.get(name, True)]

    def _agent_lifecycle_from_history(self, con: sqlite3.Connection) -> dict[str, bool]:
        try:
            hist = con.execute(
                """
                SELECT mensagem
                FROM historico
                WHERE autor='master'
                  AND (
                    mensagem LIKE 'Spawn/chat: agente % preparado com skill:%'
                    OR mensagem LIKE 'Agente encerrado por comando do usuário:%'
                    OR mensagem LIKE 'Agente encerrado antes de spawn/reuso:%'
                  )
                ORDER BY id ASC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return {}
        lifecycle: dict[str, bool] = {}
        for row in hist:
            message = str(row["mensagem"] or "")
            spawn_match = re.search(r"Spawn/chat: agente '([^']+)' preparado com skill:", message)
            if spawn_match:
                lifecycle[self._sanitize_agent_name(spawn_match.group(1))] = True
                continue
            closed_match = re.search(r"Agente encerrado (?:por comando do usuário|antes de spawn/reuso):\s*([A-Za-z0-9_-]+)", message)
            if closed_match:
                lifecycle[self._sanitize_agent_name(closed_match.group(1))] = False
        return lifecycle

    def _sync_agents_from_db(self) -> None:
        if not self.db_path.exists():
            return
        con = db_connect(self.db_path)
        try:
            db_agents = self._load_agents_from_db(con)
        finally:
            con.close()

        for name in db_agents:
            if name not in self.agents:
                self.agents.append(name)

    def _pending_approval_task(self, con: sqlite3.Connection) -> sqlite3.Row | None:
        return con.execute(
            """
            SELECT id, agente_destino, status, solicitacao, resposta, data_criacao
            FROM tarefas
            WHERE status='aguardando_aprovacao'
            ORDER BY data_criacao ASC, id ASC
            LIMIT 1
            """
        ).fetchone()

    def _is_approval_text(self, text: str) -> bool:
        normalized = text.strip().lower()
        return normalized in {"sim", "s", "autorizado", "autorizada", "prossiga", "pode prosseguir", "aprovado", "aprovar"}

    def _is_denial_text(self, text: str) -> bool:
        normalized = text.strip().lower()
        return normalized in {"nao", "não", "n", "negado", "negada", "cancelar", "cancele", "rejeitado", "rejeitar"}

    def _handle_approval_response(self, con: sqlite3.Connection, user_prompt: str) -> bool:
        task = self._pending_approval_task(con)
        if task is None:
            return False

        task_id = int(task["id"])
        agente = task["agente_destino"]
        if self._is_approval_text(user_prompt):
            con.execute(
                "UPDATE tarefas SET status='processando', resposta=? WHERE id=?",
                (f"Aprovado pelo Master em {utc_ts_ms()}. Prossiga.", task_id),
            )
            con.execute(
                "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                ("master", f"Aprovei a retomada da tarefa #{task_id} para {agente}.", utc_ts_ms()),
            )
            self.alerted_approval_task_ids.discard(task_id)
            self._print_user_response(f"Autorizado. O agente {agente} pode prosseguir na tarefa #{task_id}.")
            return True

        if self._is_denial_text(user_prompt):
            con.execute(
                "UPDATE tarefas SET status='cancelado', resposta=? WHERE id=?",
                ("Negado pelo Master. Tente outra abordagem sem a acao solicitada.", task_id),
            )
            con.execute(
                "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                ("master", f"Neguei a aprovacao da tarefa #{task_id} para {agente}; agente deve tentar outra abordagem.", utc_ts_ms()),
            )
            self.alerted_approval_task_ids.discard(task_id)
            self._print_user_response(f"Negado. A tarefa #{task_id} do agente {agente} foi marcada como cancelada.")
            return True

        return False

    def _handle_user_prompt(self, user_prompt: str) -> None:
        if not self.db_path.exists():
            self._log(f"[master] Cannot handle prompt because DB is missing: {self.db_path}")
            return

        con = db_connect(self.db_path)
        try:
            self._record_user_message(con, user_prompt)
            if self._handle_approval_response(con, user_prompt):
                return

            normalized_prompt = user_prompt.strip().lower()
            if normalized_prompt in {"restaurar agentes", "restaurar agendes", "restore agents"}:
                self._sync_agents_from_db()
                self.restaurar_agentes_ativos()
                self._print_user_response("Restauração de agentes executada. Confira os panes no tmux.")
                return
            if self._is_pause_command(user_prompt):
                self._update_project(con, status_global="Pausado")
                self._print_user_response("Execução global pausada. Os agentes não vão consumir novas tarefas até retomar.")
                con.execute(
                    "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                    ("master", "Execução global pausada por comando do usuário.", utc_ts_ms()),
                )
                return
            if self._is_resume_command(user_prompt):
                self._update_project(con, status_global="Em andamento")
                self._print_user_response("Execução global retomada. Os agentes podem voltar a processar tarefas.")
                con.execute(
                    "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                    ("master", "Execução global retomada por comando do usuário.", utc_ts_ms()),
                )
                return
            agents_to_end = self._agent_names_to_end_from_text(user_prompt)
            if agents_to_end and not any(token in normalized_prompt for token in ("criar", "novo", "nova", "spawn", "recriar", "substituir")):
                ended = [name for name in agents_to_end if self.encerrar_agente(name)]
                for name in ended:
                    con.execute(
                        "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                        ("master", f"Agente encerrado por comando do usuário: {name}", utc_ts_ms()),
                    )
                self._print_user_response("Agentes encerrados: " + ", ".join(ended) if ended else "Nenhum agente ativo encontrado para encerrar.")
                return

            fast_response = self._simple_user_response(user_prompt)
            if fast_response is not None:
                self._print_user_response(fast_response)
                con.execute(
                    "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                    ("master", fast_response, utc_ts_ms()),
                )
                con.execute(
                    "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                    ("master", "Fast-path aplicado: resposta local sem chamada de LLM.", utc_ts_ms()),
                )
                return

            projeto, historico = self._fetch_routing_context(con)
            router_historico = self._history_for_router(user_prompt, historico)
            router_prompt = self._build_user_task_prompt(user_prompt, projeto, router_historico)

            try:
                self._log("[master] Asking LLM to plan tasks for user prompt")
                output = self._generate_planner_output(router_prompt)
                decision = self._parse_swarm_decision(output)
            except Exception as e:
                self._log(f"[master] Swarm planning failed: {e}")
                decision = None
                output = ""


            if not decision:
                self._log("[master] Resposta invalida do planner. Saida bruta abaixo:")
                self._print_user_response(output or "(sem saida do LLM)")
                return

            resposta_usuario = str(decision["resposta_usuario"])
            self._print_user_response(resposta_usuario)
            con.execute(
                "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                ("master", resposta_usuario, utc_ts_ms()),
            )

            focar_agente = decision.get("focar_agente")
            if focar_agente:
                self._log(f"[master] LLM requested to focus agent '{focar_agente}'")
                self.focar_agente(focar_agente)

            if not decision["spawn"]:
                con.execute(
                    "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                    ("master", "Planner decidiu spawn=false; nenhum novo agente criado.", utc_ts_ms()),
                )
                return

            explicit_end_agents = [name for name in decision.get("encerrar_agentes", []) if name in self.agents]
            inferred_end_agents = self._agent_names_to_end_from_text(f"{resposta_usuario}\n{user_prompt}")
            for agent_to_end in list(dict.fromkeys(explicit_end_agents + inferred_end_agents)):
                if agent_to_end != decision["nome_agente"] and self.encerrar_agente(agent_to_end):
                    con.execute(
                        "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                        ("master", f"Agente encerrado antes de spawn/reuso: {agent_to_end}", utc_ts_ms()),
                    )

            agent_name = str(decision["nome_agente"])
            skill_prompt = str(decision["skill_prompt"])
            tarefa_inicial = str(decision["tarefa_inicial"])

            # Preserve explicit skill references from the user's original prompt.
            # This avoids losing a URL/ref when the planner rewrites skill_prompt.
            user_skill_refs = self._extract_skill_refs(user_prompt)
            planner_skill_refs = self._extract_skill_refs(skill_prompt)
            missing_refs = [ref for ref in user_skill_refs if ref not in planner_skill_refs]
            if missing_refs:
                refs_text = "\n".join(f"Skill reference: {ref}" for ref in missing_refs)
                skill_prompt = f"{skill_prompt.rstrip()}\n\n{refs_text}"
                self._log(f"[master] Preserved user-provided skill reference(s): {', '.join(missing_refs)}")

            if agent_name not in self.agents:
                try:
                    self.criar_novo_agente(agent_name, skill_prompt)
                    self.agents.append(agent_name)
                    self._log("[master] Waiting for dynamic agent pane to initialize...")
                    time.sleep(3)
                except subprocess.CalledProcessError as e:
                    self._log(f"[master] Failed to spawn agent '{agent_name}' with exit code {e.returncode}")
                    if e.stderr:
                        self._log(f"[master] tmux stderr: {e.stderr.strip()}")
                    return
                except FileNotFoundError:
                    self._log("[master] Failed to spawn agent: tmux not found in PATH")
                    return
            else:
                self._log(f"[master] Agent '{agent_name}' already exists; reusing it")

            con.execute("BEGIN IMMEDIATE")
            try:
                self._enqueue_task(con, agent=agent_name, solicitacao=tarefa_inicial)
                con.execute(
                    "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                    ("master", f"Spawn/chat: agente '{agent_name}' preparado com skill: {skill_prompt}", utc_ts_ms()),
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

            self._log(f"[master] Dynamic agent '{agent_name}' recebeu tarefa inicial")
        finally:
            con.close()

    def _orchestration_loop(self) -> None:
        self._log(f"[master] Background orchestration started. DB: {self.db_path}")
        while not self.stop_event.is_set():
            if not self.db_path.exists():
                self._log(f"[master] Waiting for DB to exist: {self.db_path}")
                time.sleep(self.poll_interval_s)
                continue

            try:
                con = db_connect(self.db_path)
            except Exception as e:
                self._log(f"[master] ERROR opening DB: {e}")
                time.sleep(self.poll_interval_s)
                continue

            try:
                approval_task = self._pending_approval_task(con)
                if approval_task is not None:
                    approval_task_id = int(approval_task["id"])
                    if approval_task_id not in self.alerted_approval_task_ids:
                        self.alerted_approval_task_ids.add(approval_task_id)
                        self._log(
                            "\n⚠️ [PEDIDO DE PERMISSÃO] "
                            f"O agente {approval_task['agente_destino']} pergunta: {approval_task['resposta']}"
                        )
                        self._log("Digite 'sim', 'autorizado' ou 'prossiga' para aprovar; 'não' ou 'cancelar' para negar.")

                # Pega a proxima tarefa concluida ainda nao tratada.
                task = con.execute(
                    """
                    SELECT id, agente_destino, status, solicitacao, resposta, data_criacao
                    FROM tarefas
                    WHERE status='concluido' AND master_tratada=0
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                ).fetchone()

                if task is None:
                    time.sleep(self.poll_interval_s)
                    continue

                continuation_decision = self._plan_continuation(con, task)
                if continuation_decision and not self._ensure_continuation_agent(continuation_decision):
                    continuation_decision = None

                # Transacao curta para aplicar efeitos (projeto + novas tarefas + historico)
                con.execute("BEGIN IMMEDIATE")
                try:
                    # Re-le para garantir que ainda esta concluida.
                    task2 = con.execute(
                        "SELECT id, agente_destino, status, solicitacao, resposta, data_criacao, master_tratada FROM tarefas WHERE id=?",
                        (int(task["id"]),),
                    ).fetchone()
                    if task2 is not None and task2["status"] == "concluido" and int(task2["master_tratada"]) == 0:
                        self._handle_completed_task(con, task2, continuation_decision)
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise

            except sqlite3.OperationalError as e:
                self._log(f"[master] SQLite operational error: {e}")
                time.sleep(self.poll_interval_s)
            except Exception as e:
                self._log(f"[master] ERROR: {e}")
                time.sleep(self.poll_interval_s)
            finally:
                try:
                    con.close()
                except Exception:
                    pass

        self._log("[master] Background orchestration stopped")

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self._orchestration_loop, name="master-orchestration", daemon=True)
        thread.start()
        return thread

    def run_interactive(self) -> None:
        self._log("[master] Interactive mode ready. Type 'sair', 'exit' or Ctrl+C to stop.")
        try:
            while True:
                with self.print_lock:
                    self.input_active = True
                    print("\nVocê: ", end="", flush=True)
                user_prompt = input()
                with self.print_lock:
                    self.input_active = False
                user_prompt = user_prompt.strip()
                if not user_prompt:
                    continue
                if user_prompt.lower() in {"sair", "exit", "quit"}:
                    self._log("[master] Shutting down interactive loop")
                    self.stop_event.set()
                    return
                self._handle_user_prompt(user_prompt)
        except (KeyboardInterrupt, EOFError):
            with self.print_lock:
                self.input_active = False
            self._log("\n[master] Shutting down interactive loop")
            self.stop_event.set()

    def run_forever(self) -> None:
        self._sync_agents_from_db()
        self.restaurar_agentes_ativos()
        self.start_background()
        self.run_interactive()


def parse_agents(s: str) -> list[str]:
    agents = [a.strip() for a in s.split(",") if a.strip()]
    return agents


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".")
    ap.add_argument("--agents", type=parse_agents, default=[], help="Comma-separated existing agent names")
    ap.add_argument("--poll", type=float, default=0.5)
    ap.add_argument("--seed", action="store_true")
    ap.add_argument("--tmux-session", default=None, help="tmux session used for dynamic agent panes")
    args = ap.parse_args()

    m = MasterOrchestrator(
        root=Path(args.root),
        agents=args.agents,
        poll_interval_s=args.poll,
        tmux_session=args.tmux_session,
    )
    if args.seed:
        m.seed()
    m.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
