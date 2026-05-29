#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


PRINT_LOCK = threading.Lock()


def safe_print(message: str) -> None:
    with PRINT_LOCK:
        print(f"\r{message}", flush=True)


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
        self.root = root.resolve()
        self.db_path = self.root / "squad.db"
        self.poll_interval_s = poll_interval_s
        self.agents = agents
        self.tmux_session = tmux_session
        self.project_dir = Path(__file__).resolve().parent
        self.agent_script = self.project_dir / "agent_base.py"
        self.print_lock = PRINT_LOCK
        self.stop_event = threading.Event()
        self.input_active = False
        self.alerted_approval_task_ids: set[int] = set()

    def _log(self, message: str) -> None:
        with self.print_lock:
            print(f"\r{message}", flush=True)
            if self.input_active:
                print("Você: ", end="", flush=True)

    def chamar_llm(self, prompt: str) -> str:
        result = subprocess.run(
            ["codex", "exec", "--skip-git-repo-check", "-"],
            input=prompt,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def _resolve_tmux_session(self) -> str:
        if self.tmux_session:
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
                self._log(f"[master] Detected active tmux session: {session}")
                return session

        session = os.environ.get("SESSION", "ai_squad")
        subprocess.run(["tmux", "has-session", "-t", session], capture_output=True, text=True, check=True)
        self.tmux_session = session
        self._log(f"[master] Using configured tmux session: {session}")
        return session

    def criar_novo_agente(self, nome: str, skill_prompt: str) -> None:
        session = self._resolve_tmux_session()
        command = shlex.join(
            [
                "python3",
                "-u",
                str(self.agent_script),
                "--root",
                str(self.root),
                "--name",
                nome,
                "--system-prompt",
                skill_prompt,
            ]
        )
        self._log(f"[master] Spawning dynamic agent '{nome}' in tmux session '{session}'")
        subprocess.run(
            ["tmux", "set-window-option", "-t", f"{session}:", "main-pane-width", "50%"],
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            [
                "tmux",
                "split-window",
                "-d",
                "-t",
                f"{session}:",
                "-c",
                str(self.project_dir),
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

    def _avaliar_necessidade_de_especialista(
        self,
        con: sqlite3.Connection,
        task: sqlite3.Row,
    ) -> dict[str, str] | None:
        projeto, historico = self._fetch_routing_context(con)
        prompt = self._build_specialist_decision_prompt(task, projeto, historico)
        self._log(f"[master] Asking Codex CLI if task id={task['id']} needs a dynamic specialist")
        try:
            output = self.chamar_llm(prompt)
        except subprocess.CalledProcessError as e:
            self._log(f"[master] Specialist decision skipped: Codex CLI failed with exit code {e.returncode}")
            if e.stderr:
                self._log(f"[master] Codex stderr: {e.stderr.strip()}")
            return None
        except FileNotFoundError:
            self._log("[master] Specialist decision skipped: codex CLI not found in PATH")
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
        specialist_decision: dict[str, str] | None = None,
    ) -> None:
        task_id = int(task["id"])
        agente = task["agente_destino"]
        resposta = task["resposta"] or ""

        self._log(f"[master] Handling concluded task id={task_id} from_agent={agente}")
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            ("master", f"Recebi conclusao da tarefa #{task_id} ({agente}).", utc_ts_ms()),
        )

        self._update_project(con, status_global=f"{agente}_concluido")
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            ("master", f"Agente '{agente}' concluiu tarefa #{task_id}. Aguardando proximo comando do usuario.", utc_ts_ms()),
        )
        self._log(f"[master] Agent '{agente}' completed task id={task_id}; waiting for user command")

        con.execute("UPDATE tarefas SET master_tratada=1 WHERE id=?", (task_id,))
        self._log(f"[master] Marked task id={task_id} as master_tratada=1")

    def _build_user_task_prompt(self, user_prompt: str, projeto: dict[str, Any], historico: list[dict[str, Any]]) -> str:
        return (
            "Voce e o Master de um Dynamic Swarm local.\n"
            "O sistema inicia apenas com o Master. Novos agentes devem nascer somente quando o usuario pedir ou quando a tarefa exigir claramente uma skill dedicada.\n"
            "Analise a mensagem do usuario e decida se deve criar um novo agente tmux.\n"
            "Responda sempre somente JSON valido, sem markdown, no formato:\n"
            '{"spawn": true, "nome_agente": "backend_login", "skill_prompt": "Voce e especialista em backend...", "tarefa_inicial": "Implemente...", "resposta_usuario": "Vou criar um agente backend para cuidar disso."}\n\n'
            "Regras:\n"
            "- resposta_usuario e obrigatoria e deve conter a mensagem conversacional que sera exibida ao usuario.\n"
            "- Use spawn=true quando o usuario pedir para criar/chamar um agente ou quando a solicitacao exigir execucao por especialista.\n"
            "- Use spawn=false se a mensagem for apenas conversa, status, pergunta simples, ou nao exigir novo agente.\n"
            "- Se o usuario disser apenas ola/oi, responda cordialmente em resposta_usuario, use spawn=false e aguarde novo comando.\n"
            "- Se precisar de mais contexto/permissao antes de criar tarefa, pergunte em resposta_usuario, use spawn=false e aguarde.\n"
            "- nome_agente deve ser curto, em snake_case, sem espacos, acentos ou simbolos.\n"
            "- skill_prompt deve descrever a especialidade, responsabilidades e estilo de trabalho do agente.\n"
            "- tarefa_inicial deve ser a primeira tarefa concreta a inserir na fila do novo agente.\n"
            "- Se spawn=false, deixe nome_agente, skill_prompt e tarefa_inicial como strings vazias, mas preencha resposta_usuario.\n"
            "- Nao inclua explicacoes fora do JSON.\n\n"
            f"AGENTES_EXISTENTES:\n{json.dumps(self.agents, ensure_ascii=True)}\n\n"
            f"PROJETO:\n{json.dumps(projeto, ensure_ascii=True)}\n\n"
            f"HISTORICO_RECENTE:\n{json.dumps(historico, ensure_ascii=True)}\n\n"
            f"SOLICITACAO_DO_USUARIO:\n{user_prompt}\n"
        )

    def _parse_swarm_decision(self, output: str) -> dict[str, Any] | None:
        parsed = self._extract_json_object(output)
        if not parsed:
            return None

        spawn = bool(parsed.get("spawn"))
        nome_agente = self._sanitize_agent_name(str(parsed.get("nome_agente") or ""))
        skill_prompt = str(parsed.get("skill_prompt") or "").strip()
        tarefa_inicial = str(parsed.get("tarefa_inicial") or "").strip()
        resposta_usuario = str(parsed.get("resposta_usuario") or "").strip()

        if not resposta_usuario:
            return None

        if not spawn:
            return {
                "spawn": False,
                "nome_agente": "",
                "skill_prompt": "",
                "tarefa_inicial": "",
                "resposta_usuario": resposta_usuario,
            }
        if not nome_agente or not skill_prompt or not tarefa_inicial:
            return None
        return {
            "spawn": True,
            "nome_agente": nome_agente,
            "skill_prompt": skill_prompt,
            "tarefa_inicial": tarefa_inicial,
            "resposta_usuario": resposta_usuario,
        }

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

            projeto, historico = self._fetch_routing_context(con)
            router_prompt = self._build_user_task_prompt(user_prompt, projeto, historico)

            try:
                self._log("[master] Asking Codex CLI to plan tasks for user prompt")
                output = self.chamar_llm(router_prompt)
                decision = self._parse_swarm_decision(output)
            except subprocess.CalledProcessError as e:
                self._log(f"[master] Swarm planning failed with exit code {e.returncode}")
                if e.stderr:
                    self._log(f"[master] Codex stderr: {e.stderr.strip()}")
                if e.stdout:
                    self._log(f"[master] Codex stdout: {e.stdout.strip()}")
                decision = None
                output = ""
            except FileNotFoundError:
                self._log("[master] Swarm planning skipped: codex CLI not found")
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

            if not decision["spawn"]:
                con.execute(
                    "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
                    ("master", "Planner decidiu spawn=false; nenhum novo agente criado.", utc_ts_ms()),
                )
                return

            agent_name = str(decision["nome_agente"])
            skill_prompt = str(decision["skill_prompt"])
            tarefa_inicial = str(decision["tarefa_inicial"])
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

                # Transacao curta para aplicar efeitos (projeto + novas tarefas + historico)
                con.execute("BEGIN IMMEDIATE")
                try:
                    # Re-le para garantir que ainda esta concluida.
                    task2 = con.execute(
                        "SELECT id, agente_destino, status, solicitacao, resposta, data_criacao, master_tratada FROM tarefas WHERE id=?",
                        (int(task["id"]),),
                    ).fetchone()
                    if task2 is not None and task2["status"] == "concluido" and int(task2["master_tratada"]) == 0:
                        self._handle_completed_task(con, task2)
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
        self.start_background()
        self.run_interactive()


def parse_agents(s: str) -> list[str]:
    agents = [a.strip() for a in s.split(",") if a.strip()]
    return agents


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="workspace")
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
