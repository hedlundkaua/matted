#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


APPROVAL_PATTERNS = (
    "approval required",
    "requires approval",
    "requesting approval",
    "ask for approval",
    "permission",
    "permissao",
    "permissão",
    "autorizacao",
    "autorização",
    "overwrite",
    "sobrescrever",
    "dangerous",
    "destructive",
    "comando perigoso",
)


PRINT_LOCK = threading.Lock()


def utc_ts_ms() -> int:
    return int(time.time() * 1000)


def db_connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=5.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=5000;")
    return con


@dataclass
class WorkerAgent:
    name: str
    system_prompt: str
    root: Path = Path("workspace")
    poll_interval_s: float = 0.5
    history_tail: int = 20

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.db_path = self.root / "squad.db"
        self.stop_event = threading.Event()
        self.print_lock = PRINT_LOCK
        self.input_active = False

    def _log(self, message: str) -> None:
        with self.print_lock:
            print(f"\r{message}", flush=True)
            if self.input_active:
                print(f"Você (para {self.name}): ", end="", flush=True)

    def call_llm(self, prompt: str) -> str:
        cmd = ["codex", "exec", "--skip-git-repo-check", "-"]
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def stream_pipe(pipe: Any, sink: list[str]) -> None:
            try:
                while True:
                    line = pipe.readline()
                    if line == "":
                        break
                    print(line, end="", flush=True)
                    sink.append(line)
            finally:
                pipe.close()

        stdout_thread = threading.Thread(target=stream_pipe, args=(process.stdout, stdout_lines), daemon=True)
        stderr_thread = threading.Thread(target=stream_pipe, args=(process.stderr, stderr_lines), daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        assert process.stdin is not None
        process.stdin.write(prompt)
        process.stdin.close()

        return_code = process.wait()
        stdout_thread.join()
        stderr_thread.join()

        output_completo = "".join(stdout_lines)
        stderr_completo = "".join(stderr_lines)

        if return_code != 0:
            raise subprocess.CalledProcessError(
                return_code,
                cmd,
                output=output_completo,
                stderr=stderr_completo,
            )

        return output_completo.strip()

    def _extract_json_object(self, text: str) -> dict[str, Any] | None:
        text = text.strip()
        if not text:
            return None
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            return None

    def _fetch_context(self, con: sqlite3.Connection) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        proj = con.execute(
            "SELECT id, status_global, tecnologias, ultima_atualizacao FROM projeto WHERE id=1"
        ).fetchone()
        projeto = dict(proj) if proj else {}
        hist = con.execute(
            "SELECT id, autor, mensagem, timestamp FROM historico ORDER BY timestamp DESC, id DESC LIMIT ?",
            (self.history_tail,),
        ).fetchall()
        historico = [dict(r) for r in reversed(hist)]
        return projeto, historico

    def _build_prompt(self, task: sqlite3.Row, projeto: dict[str, Any], historico: list[dict[str, Any]]) -> str:
        status_global = projeto.get("status_global", "desconhecido")
        return (
            f"SYSTEM:\n{self.system_prompt}\n\n"
            "Se precisar executar uma acao destrutiva, sobrescrever arquivos, instalar dependencias, "
            "ou pedir permissao humana, responda claramente com uma pergunta de aprovacao antes de prosseguir.\n\n"
            f"STATUS_ATUAL_DO_PROJETO:\n{status_global}\n\n"
            f"PROJETO_COMPLETO:\n{json.dumps(projeto, ensure_ascii=True)}\n\n"
            f"HISTORICO_ULTIMAS_{self.history_tail}:\n{json.dumps(historico, ensure_ascii=True)}\n\n"
            f"SOLICITACAO_DA_TAREFA:\n{task['solicitacao']}\n"
        )

    def _build_interactive_prompt(self, user_prompt: str, projeto: dict[str, Any], historico: list[dict[str, Any]]) -> str:
        return (
            f"SYSTEM:\n{self.system_prompt}\n\n"
            "Voce esta conversando diretamente com o usuario na sua pane tmux.\n"
            "Responda sempre somente JSON valido, sem markdown, no formato:\n"
            '{"resposta_usuario": "texto para mostrar ao usuario", "novas_tarefas": []}\n\n'
            "Regras:\n"
            "- resposta_usuario e obrigatoria e deve conter a resposta conversacional para o usuario.\n"
            "- novas_tarefas e opcional; use [] quando nao precisar pedir ajuda a outro agente.\n"
            "- Para pedir ajuda, use novas_tarefas como lista de objetos com agente_destino e solicitacao.\n"
            "- Nao inclua texto fora do JSON.\n\n"
            f"AGENTE_ATUAL:\n{self.name}\n\n"
            f"PROJETO:\n{json.dumps(projeto, ensure_ascii=True)}\n\n"
            f"HISTORICO_ULTIMAS_{self.history_tail}:\n{json.dumps(historico, ensure_ascii=True)}\n\n"
            f"MENSAGEM_DO_USUARIO:\n{user_prompt}\n"
        )

    def _claim_oldest_task(self, con: sqlite3.Connection) -> sqlite3.Row | None:
        # Claim atômico (SQLite 3.35+): evita dois processos pegarem a mesma tarefa.
        row = con.execute(
            """
            UPDATE tarefas
            SET status='processando'
            WHERE id = (
              SELECT id FROM tarefas
              WHERE agente_destino=? AND status='pendente'
              ORDER BY data_criacao ASC, id ASC
              LIMIT 1
            )
            RETURNING id, agente_destino, status, solicitacao, resposta, data_criacao;
            """,
            (self.name,),
        ).fetchone()
        return row

    def _finish_task(self, con: sqlite3.Connection, task_id: int, answer_text: str) -> None:
        con.execute(
            "UPDATE tarefas SET resposta=?, status='concluido' WHERE id=?",
            (answer_text, task_id),
        )
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            (self.name, f"Entreguei tarefa #{task_id}: {answer_text}", utc_ts_ms()),
        )

    def _enqueue_task(self, con: sqlite3.Connection, *, agent: str, solicitacao: str) -> int:
        now = utc_ts_ms()
        cur = con.execute(
            "INSERT INTO tarefas (agente_destino, status, solicitacao, resposta, data_criacao) VALUES (?,?,?,?,?)",
            (agent, "pendente", solicitacao, None, now),
        )
        task_id = int(cur.lastrowid)
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            (self.name, f"Enfileirei tarefa #{task_id} para {agent}: {solicitacao}", now),
        )
        self._log(f"[{self.name}] Enqueued task id={task_id} -> {agent}")
        return task_id

    def _fail_task(self, con: sqlite3.Connection, task_id: int, error_text: str) -> None:
        con.execute(
            "UPDATE tarefas SET resposta=?, status='erro' WHERE id=?",
            (error_text, task_id),
        )
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            (self.name, f"Falhei na tarefa #{task_id}: {error_text}", utc_ts_ms()),
        )

    def _request_approval(self, con: sqlite3.Connection, task_id: int, question: str) -> None:
        con.execute(
            "UPDATE tarefas SET resposta=?, status='aguardando_aprovacao' WHERE id=?",
            (question, task_id),
        )
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            (self.name, f"Solicitei aprovacao para tarefa #{task_id}: {question}", utc_ts_ms()),
        )

    def _wait_for_approval_decision(self, con: sqlite3.Connection, task_id: int) -> str:
        self._log(f"[{self.name}] Waiting approval decision for task id={task_id}...")
        while True:
            row = con.execute("SELECT status FROM tarefas WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return "cancelado"

            status = str(row["status"])
            if status in {"processando", "cancelado"}:
                self._log(f"[{self.name}] Approval decision for task id={task_id}: {status}")
                return status
            time.sleep(self.poll_interval_s)

    def _looks_like_approval_request(self, text: str) -> bool:
        lowered = text.lower()
        return any(pattern in lowered for pattern in APPROVAL_PATTERNS)

    def _approval_question_from_error(self, task_error: subprocess.CalledProcessError) -> str:
        stderr = (task_error.stderr or "").strip()
        stdout = (task_error.stdout or "").strip()
        details = stderr or stdout or str(task_error)
        return f"O Codex CLI pediu permissao ou bloqueou uma acao. Aprovar retomada? Detalhes: {details}"

    def _parse_interactive_response(self, output: str) -> tuple[str, list[dict[str, str]]] | None:
        parsed = self._extract_json_object(output)
        if not parsed:
            return None

        resposta_usuario = str(parsed.get("resposta_usuario") or "").strip()
        if not resposta_usuario:
            return None

        novas_tarefas: list[dict[str, str]] = []
        raw_tasks = parsed.get("novas_tarefas", [])
        if isinstance(raw_tasks, list):
            for item in raw_tasks:
                if not isinstance(item, dict):
                    continue
                agente_destino = str(item.get("agente_destino") or "").strip()
                solicitacao = str(item.get("solicitacao") or "").strip()
                if agente_destino and solicitacao:
                    novas_tarefas.append({"agente_destino": agente_destino, "solicitacao": solicitacao})

        return resposta_usuario, novas_tarefas

    def _print_user_response(self, message: str) -> None:
        with self.print_lock:
            print(f"\n{self.name}:")
            print(message.strip())

    def _handle_interactive_prompt(self, user_prompt: str) -> None:
        if not self.db_path.exists():
            self._log(f"[{self.name}] Cannot handle prompt because DB is missing: {self.db_path}")
            return

        con = db_connect(self.db_path)
        try:
            con.execute(
                "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                (f"usuario->{self.name}", user_prompt, utc_ts_ms()),
            )
            projeto, historico = self._fetch_context(con)
            prompt = self._build_interactive_prompt(user_prompt, projeto, historico)

            try:
                output = self.call_llm(prompt)
                parsed = self._parse_interactive_response(output)
            except subprocess.CalledProcessError as e:
                stderr = (e.stderr or "").strip()
                stdout = (e.stdout or "").strip()
                if stderr:
                    self._log(f"[{self.name}] Codex stderr: {stderr}")
                if stdout:
                    self._log(f"[{self.name}] Codex stdout before failure: {stdout}")
                parsed = (f"Falhei ao chamar o Codex CLI: exit status {e.returncode}.", [])
            except FileNotFoundError:
                parsed = ("Nao encontrei o executavel codex no PATH desta pane.", [])

            if parsed is None:
                self._log(f"[{self.name}] Resposta invalida do LLM. Saida bruta abaixo:")
                self._print_user_response(output or "(sem saida do LLM)")
                return

            resposta_usuario, novas_tarefas = parsed
            self._print_user_response(resposta_usuario)
            con.execute(
                "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                (self.name, resposta_usuario, utc_ts_ms()),
            )
            for task in novas_tarefas:
                self._enqueue_task(con, agent=task["agente_destino"], solicitacao=task["solicitacao"])
        finally:
            con.close()

    def _task_loop(self) -> None:
        self._log(f"[{self.name}] Background task loop started. DB: {self.db_path}")
        while not self.stop_event.is_set():
            if not self.db_path.exists():
                self._log(f"[{self.name}] Waiting for DB to exist: {self.db_path}")
                time.sleep(self.poll_interval_s)
                continue

            try:
                con = db_connect(self.db_path)
            except Exception as e:
                self._log(f"[{self.name}] ERROR opening DB: {e}")
                time.sleep(self.poll_interval_s)
                continue

            try:
                task = self._claim_oldest_task(con)
                if task is None:
                    time.sleep(self.poll_interval_s)
                    continue

                self._log(f"[{self.name}] Claimed task id={task['id']} status=processando")
                task_id = int(task["id"])
                try:
                    projeto, historico = self._fetch_context(con)
                    prompt = self._build_prompt(task, projeto, historico)
                    self._log(f"[{self.name}] Processing task id={task_id}...")
                    while True:
                        try:
                            answer_text = self.call_llm(prompt)
                            if self._looks_like_approval_request(answer_text):
                                self._request_approval(con, task_id, answer_text)
                                decision = self._wait_for_approval_decision(con, task_id)
                                if decision == "processando":
                                    prompt = (
                                        f"{prompt}\n\nAPROVACAO_HUMANA:\n"
                                        "O Master aprovou a acao solicitada. Continue a tarefa usando a abordagem aprovada.\n"
                                    )
                                    continue
                                self._log(f"[{self.name}] Task id={task_id} canceled by Master")
                                break

                            self._finish_task(con, task_id, answer_text)
                            self._log(f"[{self.name}] Completed task id={task_id} (status=concluido)")
                            break
                        except subprocess.CalledProcessError as task_error:
                            stderr = (task_error.stderr or "").strip()
                            stdout = (task_error.stdout or "").strip()
                            combined = f"{stderr}\n{stdout}"
                            if self._looks_like_approval_request(combined):
                                question = self._approval_question_from_error(task_error)
                                self._request_approval(con, task_id, question)
                                decision = self._wait_for_approval_decision(con, task_id)
                                if decision == "processando":
                                    prompt = (
                                        f"{prompt}\n\nAPROVACAO_HUMANA:\n"
                                        "O Master aprovou a acao solicitada. Continue a tarefa usando a abordagem aprovada.\n"
                                    )
                                    continue
                                self._log(f"[{self.name}] Task id={task_id} canceled by Master")
                                break
                            raise
                except subprocess.CalledProcessError as task_error:
                    stderr = (task_error.stderr or "").strip()
                    stdout = (task_error.stdout or "").strip()
                    if stderr:
                        self._log(f"[{self.name}] Codex stderr for task id={task_id}: {stderr}")
                    if stdout:
                        self._log(f"[{self.name}] Codex stdout before failure for task id={task_id}: {stdout}")
                    error_text = f"{type(task_error).__name__}: exit status {task_error.returncode}"
                    if stderr:
                        error_text = f"{error_text}: {stderr}"
                    self._fail_task(con, task_id, error_text)
                    self._log(f"[{self.name}] Failed task id={task_id} (status=erro): {error_text}")
                except Exception as task_error:
                    error_text = f"{type(task_error).__name__}: {task_error}"
                    self._fail_task(con, task_id, error_text)
                    self._log(f"[{self.name}] Failed task id={task_id} (status=erro): {error_text}")
            except sqlite3.OperationalError as e:
                # Usually "database is locked". busy_timeout handles most cases.
                self._log(f"[{self.name}] SQLite operational error: {e}")
                time.sleep(self.poll_interval_s)
            except Exception as e:
                self._log(f"[{self.name}] ERROR: {e}")
                time.sleep(self.poll_interval_s)
            finally:
                try:
                    con.close()
                except Exception:
                    pass

        self._log(f"[{self.name}] Background task loop stopped")

    def start_background(self) -> threading.Thread:
        thread = threading.Thread(target=self._task_loop, name=f"{self.name}-task-loop", daemon=True)
        thread.start()
        return thread

    def run_interactive(self) -> None:
        self._log(f"[{self.name}] Interactive mode ready. Type 'sair', 'exit' or Ctrl+C to stop.")
        try:
            while True:
                with self.print_lock:
                    self.input_active = True
                    print(f"\nVocê (para {self.name}): ", end="", flush=True)
                user_prompt = input()
                with self.print_lock:
                    self.input_active = False

                user_prompt = user_prompt.strip()
                if not user_prompt:
                    continue
                if user_prompt.lower() in {"sair", "exit", "quit"}:
                    self._log(f"[{self.name}] Shutting down interactive loop")
                    self.stop_event.set()
                    return
                self._handle_interactive_prompt(user_prompt)
        except (KeyboardInterrupt, EOFError):
            with self.print_lock:
                self.input_active = False
            self._log(f"\n[{self.name}] Shutting down interactive loop")
            self.stop_event.set()

    def run_forever(self) -> None:
        self.start_background()
        self.run_interactive()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="Agent name (e.g., backend)")
    ap.add_argument("--system-prompt", default="You are a helpful AI worker.")
    ap.add_argument("--root", default="workspace")
    ap.add_argument("--poll", type=float, default=0.5, help="Polling interval in seconds")
    ap.add_argument("--history-tail", type=int, default=20, help="How many history rows to include")
    args = ap.parse_args()

    agent = WorkerAgent(
        name=args.name,
        system_prompt=args.system_prompt,
        root=Path(args.root),
        poll_interval_s=args.poll,
        history_tail=args.history_tail,
    )
    agent.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
