#!/usr/bin/env python3

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from llm.factory import ProviderFactory
from terminal_colors import color_text, strip_leading_bracket_name



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

DEFAULT_CODEX_SANDBOX = "workspace-write"
DEFAULT_CODEX_APPROVAL = "on-request"
DEFAULT_CODEX_VERBOSE = "0"
DEFAULT_CODEX_STREAM = "filtered"
DEFAULT_LEGACY_JSON_TOOLS = "0"
DEFAULT_OPENROUTER_LOCAL_TOOLS = "1"
DEFAULT_OPENROUTER_COMMAND_TIMEOUT_S = "120"
DEFAULT_OPENROUTER_ALLOW_DESTRUCTIVE = "0"
DEFAULT_OPENROUTER_APPROVALS = "1"
DEFAULT_WORKER_CONTEXT_MODE = "adaptive"


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
    root: Path = Path(".")
    poll_interval_s: float = 0.5
    history_tail: int = 20

    def __post_init__(self) -> None:
        self.root_dir = self.root.resolve()
        self.root = self.root_dir
        self.db_path = self.root_dir / "squad.db"
        self.stop_event = threading.Event()
        self.print_lock = PRINT_LOCK
        self.input_active = False
        self.allow_all_file_edits_session = False
        self.allow_all_commands_session = False
        # Initialize the LLM provider via the factory
        self.llm_provider = ProviderFactory.get_provider(root_dir=str(self.root_dir))


    def _log(self, message: str) -> None:
        with self.print_lock:
            print(f"\r{strip_leading_bracket_name(message)}", flush=True)
            if self.input_active:
                print(f"Você (para {color_text(self.name, self.name)}): ", end="", flush=True)

    def _resolve_safe_path(self, caminho: str) -> Path | None:
        raw = str(caminho or "").strip()
        if not raw:
            return None

        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.root_dir / path

        try:
            resolved = path.resolve()
            resolved.relative_to(self.root_dir)
        except (OSError, ValueError):
            return None
        return resolved

    def ler_arquivo(self, caminho: str) -> str:
        path = self._resolve_safe_path(caminho)
        if path is None:
            return f"Erro: caminho invalido ou fora do projeto: {caminho}"
        if not path.exists():
            return f"Erro: arquivo nao encontrado: {path}"
        if not path.is_file():
            return f"Erro: o caminho nao e um arquivo: {path}"
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"Erro ao ler arquivo {path}: {e}"

    def listar_diretorio(self, caminho: str) -> str:
        path = self._resolve_safe_path(caminho)
        if path is None:
            return f"Erro: caminho invalido ou fora do projeto: {caminho}"
        if not path.exists():
            return f"Erro: diretorio nao encontrado: {path}"
        if not path.is_dir():
            return f"Erro: o caminho nao e um diretorio: {path}"
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as e:
            return f"Erro ao listar diretorio {path}: {e}"

        if not entries:
            return "(diretorio vazio)"
        return "\n".join(f"{'[dir]' if entry.is_dir() else '[file]'} {entry.name}" for entry in entries)

    def criar_arquivo(self, caminho: str, conteudo: str, sobrescrever: bool = False) -> str:
        path = self._resolve_safe_path(caminho)
        if path is None:
            return f"Erro: caminho invalido ou fora do projeto: {caminho}"
        if path.exists() and path.is_dir():
            return f"Erro: o caminho aponta para um diretorio: {path}"
        existed_before = path.exists()
        if path.exists() and not sobrescrever:
            return (
                f"Erro: arquivo ja existe: {path}. "
                "Para substituir, chame criar_arquivo com sobrescrever=true."
            )

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(conteudo), encoding="utf-8")
        except OSError as e:
            return f"Erro ao criar arquivo {path}: {e}"

        action = "sobrescrito" if existed_before else "criado"
        return f"Arquivo {action} com sucesso: {path}"

    def editar_arquivo(self, caminho: str, procurar: str, substituir: str, ocorrencias: int = 1) -> str:
        path = self._resolve_safe_path(caminho)
        if path is None:
            return f"Erro: caminho invalido ou fora do projeto: {caminho}"
        if not path.is_file():
            return f"Erro: arquivo nao encontrado: {path}"
        if not procurar:
            return "Erro: campo procurar nao pode ser vazio."

        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"Erro ao ler arquivo {path}: {e}"

        count = -1 if ocorrencias <= 0 else ocorrencias
        updated = original.replace(procurar, substituir, count)
        if updated == original:
            return f"Erro: trecho nao encontrado em {path}"

        try:
            path.write_text(updated, encoding="utf-8")
        except OSError as e:
            return f"Erro ao editar arquivo {path}: {e}"

        changed = original.count(procurar) if count == -1 else min(original.count(procurar), count)
        return f"Arquivo editado com sucesso: {path} ({changed} ocorrencia(s) substituida(s))"

    def _command_timeout_s(self) -> float:
        try:
            return float(os.environ.get("MATTED_OPENROUTER_COMMAND_TIMEOUT", DEFAULT_OPENROUTER_COMMAND_TIMEOUT_S))
        except ValueError:
            return float(DEFAULT_OPENROUTER_COMMAND_TIMEOUT_S)

    def _allow_destructive_commands(self) -> bool:
        raw = os.environ.get("MATTED_OPENROUTER_ALLOW_DESTRUCTIVE", DEFAULT_OPENROUTER_ALLOW_DESTRUCTIVE)
        return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}

    def _looks_destructive_command(self, comando: str) -> bool:
        lowered = f" {comando.strip().lower()} "
        destructive_tokens = (
            " rm ",
            " rm -",
            " rmdir ",
            " sudo ",
            " chmod ",
            " chown ",
            " mkfs",
            " dd ",
            " git reset --hard",
            " git clean",
            " kill ",
            " pkill ",
        )
        return any(token in lowered for token in destructive_tokens)

    def executar_comando(self, comando: str, allow_destructive: bool = False) -> str:
        raw = str(comando or "").strip()
        if not raw:
            return "Erro: comando vazio."
        if self._looks_destructive_command(raw) and not (allow_destructive or self._allow_destructive_commands()):
            return (
                "Erro: comando potencialmente destrutivo bloqueado. "
                "Defina MATTED_OPENROUTER_ALLOW_DESTRUCTIVE=1 se quiser permitir esse tipo de acao."
            )

        try:
            parts = shlex.split(raw)
        except ValueError as e:
            return f"Erro ao parsear comando: {e}"
        if not parts:
            return "Erro: comando vazio."

        try:
            result = subprocess.run(
                parts,
                cwd=str(self.root_dir),
                capture_output=True,
                text=True,
                timeout=self._command_timeout_s(),
                check=False,
            )
        except FileNotFoundError:
            return f"Erro: executavel nao encontrado: {parts[0]}"
        except subprocess.TimeoutExpired:
            return f"Erro: comando excedeu timeout de {self._command_timeout_s()}s: {raw}"
        except OSError as e:
            return f"Erro ao executar comando: {e}"

        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        output = (
            f"exit_code={result.returncode}\n"
            f"stdout:\n{stdout[:12000] if stdout else '(vazio)'}\n"
            f"stderr:\n{stderr[:12000] if stderr else '(vazio)'}"
        )
        return output

    def _tools_spec(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "ler_arquivo",
                    "description": "Le o conteudo textual de um arquivo dentro do diretorio raiz do projeto.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "caminho": {
                                "type": "string",
                                "description": "Caminho relativo ao projeto ou caminho absoluto dentro do projeto.",
                            }
                        },
                        "required": ["caminho"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "listar_diretorio",
                    "description": "Lista arquivos e subdiretorios de uma pasta dentro do diretorio raiz do projeto.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "caminho": {
                                "type": "string",
                                "description": "Caminho relativo ao projeto ou caminho absoluto dentro do projeto.",
                            }
                        },
                        "required": ["caminho"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "criar_arquivo",
                    "description": "Cria um arquivo texto dentro da raiz do projeto. Cria diretorios pais automaticamente.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "caminho": {
                                "type": "string",
                                "description": "Caminho relativo ao projeto ou caminho absoluto dentro do projeto.",
                            },
                            "conteudo": {
                                "type": "string",
                                "description": "Conteudo completo que deve ser gravado no arquivo.",
                            },
                            "sobrescrever": {
                                "type": "boolean",
                                "description": "Use true apenas quando for intencional substituir um arquivo existente.",
                            },
                        },
                        "required": ["caminho", "conteudo"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "editar_arquivo",
                    "description": "Edita um arquivo textual substituindo um trecho exato por outro dentro da raiz do projeto.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "caminho": {
                                "type": "string",
                                "description": "Caminho relativo ao projeto ou caminho absoluto dentro do projeto.",
                            },
                            "procurar": {
                                "type": "string",
                                "description": "Trecho exato a localizar no arquivo.",
                            },
                            "substituir": {
                                "type": "string",
                                "description": "Novo conteudo para substituir o trecho encontrado.",
                            },
                            "ocorrencias": {
                                "type": "integer",
                                "description": "Quantidade maxima de substituicoes. Use 0 para substituir todas.",
                            },
                        },
                        "required": ["caminho", "procurar", "substituir"],
                        "additionalProperties": False,
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "executar_comando",
                    "description": "Executa um comando local na raiz do projeto e retorna stdout, stderr e exit code. Use para testes, lint, busca e comandos de desenvolvimento.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "comando": {
                                "type": "string",
                                "description": "Comando sem pipes/redirecionamentos de shell. Ex: '.venv/bin/python -m pytest -q'.",
                            }
                        },
                        "required": ["comando"],
                        "additionalProperties": False,
                    },
                },
            },
        ]

    def _tool_prompt(self, prompt: str, tool_responses: list[dict[str, Any]]) -> str:
        tool_context = ""
        if tool_responses:
            tool_context = (
                "\n\nTOOL_RESPONSES_EXECUTADAS:\n"
                f"{json.dumps(tool_responses, ensure_ascii=True)}\n\n"
                "Voce recebeu o resultado das ferramentas. "
                "Agora voce DEVE continuar a tarefa original usando os resultados acima. "
                "Nao solicite a mesma ferramenta novamente se o resultado ja estiver presente. "
                "Forneca a resposta final no padrao estabelecido. "
                "Responda exatamente no formato que o pedido original exige, sem tool_calls.\n"
            )

        return (
            f"{prompt}\n\n"
            "FERRAMENTAS_DISPONIVEIS:\n"
            f"{json.dumps(self._tools_spec(), ensure_ascii=True)}\n\n"
            "Voce pode usar ferramentas locais para ler, criar, editar arquivos e executar comandos antes de responder. "
            "Quando precisar de uma ferramenta, responda somente JSON valido, sem markdown, neste formato:\n"
            '{"tool_calls":[{"id":"call_1","type":"function","function":{"name":"ler_arquivo","arguments":{"caminho":"README.md"}}}]}\n'
            "Tambem e aceito arguments como string JSON. "
            "Para criar arquivos, use criar_arquivo com caminho relativo a raiz do projeto e conteudo completo. "
            "Para editar, prefira editar_arquivo quando puder substituir um trecho exato. "
            "Para validar, use executar_comando com comandos diretos como testes ou lint. "
            "Nao delegue criacao de arquivos para agente_destino master quando a ferramenta criar_arquivo puder fazer isso. "
            "Se voce utilizar ferramentas, aguarde o resultado delas. "
            "Apos receber o resultado, voce DEVE continuar a sua tarefa original e fornecer a resposta final "
            "no padrao estabelecido. "
            "Use apenas as ferramentas listadas. Depois que receber TOOL_RESPONSES_EXECUTADAS, "
            "produza a resposta final no formato pedido originalmente, sem o wrapper tool_calls."
            f"{tool_context}"
        )

    def _use_legacy_json_tools(self) -> bool:
        raw = os.environ.get("MATTED_LEGACY_JSON_TOOLS", DEFAULT_LEGACY_JSON_TOOLS)
        return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}

    def _use_openrouter_local_tools(self) -> bool:
        raw = os.environ.get("MATTED_OPENROUTER_LOCAL_TOOLS", DEFAULT_OPENROUTER_LOCAL_TOOLS)
        enabled = raw.strip().lower() in {"1", "true", "yes", "sim", "on"}
        return enabled and self.llm_provider.__class__.__name__ == "OpenRouterProvider"

    def _use_local_json_tools(self) -> bool:
        return self._use_legacy_json_tools() or self._use_openrouter_local_tools()

    def _openrouter_approvals_enabled(self) -> bool:
        raw = os.environ.get("MATTED_OPENROUTER_APPROVALS", DEFAULT_OPENROUTER_APPROVALS)
        return raw.strip().lower() in {"1", "true", "yes", "sim", "on"}

    def _parse_tool_calls(self, output: str) -> list[dict[str, Any]]:
        parsed = self._extract_json_object(output)
        if not parsed:
            return self._parse_longcat_tool_calls(output)

        raw_calls = parsed.get("tool_calls")
        if not isinstance(raw_calls, list):
            return []

        calls: list[dict[str, Any]] = []
        for index, raw_call in enumerate(raw_calls, start=1):
            if not isinstance(raw_call, dict):
                continue

            function_data = raw_call.get("function")
            if not isinstance(function_data, dict):
                continue

            name = str(function_data.get("name") or "").strip()
            raw_args = function_data.get("arguments", {})
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except json.JSONDecodeError:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}

            calls.append(
                {
                    "id": str(raw_call.get("id") or f"call_{index}"),
                    "type": str(raw_call.get("type") or "function"),
                    "function": {"name": name, "arguments": args},
                }
            )
        return calls

    def _parse_longcat_tool_calls(self, output: str) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        pattern = re.compile(r"<longcat_tool_call>\s*([A-Za-z0-9_]+)(.*?)(?:</longcat_tool_call>|$)", re.DOTALL)
        arg_pattern = re.compile(
            r"<longcat_arg_key>(.*?)</longcat_arg_key>\s*<longcat_arg_value>(.*?)</longcat_arg_value>",
            re.DOTALL,
        )
        for index, match in enumerate(pattern.finditer(output), start=1):
            name = match.group(1).strip()
            body = match.group(2)
            args: dict[str, Any] = {}
            for arg_match in arg_pattern.finditer(body):
                key = arg_match.group(1).strip()
                value = arg_match.group(2).strip()
                if key:
                    args[key] = value
            calls.append(
                {
                    "id": f"longcat_{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                }
            )
        return calls

    def _relative_display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.root_dir))
        except ValueError:
            return str(path)

    def _limited_unified_diff(self, before: str, after: str, path: str, max_lines: int = 80) -> str:
        diff_lines = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile=f"{path} (before)",
                tofile=f"{path} (after)",
                lineterm="",
            )
        )
        if len(diff_lines) > max_lines:
            return "\n".join(diff_lines[:max_lines] + [f"... diff truncated ({len(diff_lines) - max_lines} more lines)"])
        return "\n".join(diff_lines) if diff_lines else "(no textual diff)"

    def _approval_choice(self, *, title: str, target: str, preview: str, question: str, allow_all_label: str) -> str:
        if not self._openrouter_approvals_enabled():
            return "yes"

        separator = "-" * 100
        with self.print_lock:
            self.input_active = True
            print()
            print(separator)
            print(f" {title}")
            if target:
                print(f" {target}")
            print(separator)
            if preview.strip():
                print(preview.rstrip())
                print(separator)
            print(f" {question}")
            print(" > 1. Yes")
            print(f"   2. {allow_all_label}")
            print("   3. No")
            print("Choice [1-3]: ", end="", flush=True)

        try:
            choice = input().strip()
        except (EOFError, KeyboardInterrupt):
            choice = "3"
        finally:
            with self.print_lock:
                self.input_active = False

        if choice == "2":
            return "all"
        if choice == "1" or choice == "":
            return "yes"
        return "no"

    def _approve_file_edit(self, *, action: str, path: Path, before: str, after: str) -> bool:
        if self.allow_all_file_edits_session:
            return True
        display_path = self._relative_display_path(path)
        preview = self._limited_unified_diff(before, after, display_path)
        choice = self._approval_choice(
            title=action,
            target=display_path,
            preview=preview,
            question=f"Do you want to make this edit to {display_path}?",
            allow_all_label="Yes, allow all edits during this session",
        )
        if choice == "all":
            self.allow_all_file_edits_session = True
            return True
        return choice == "yes"

    def _approve_command(self, comando: str) -> bool:
        if self.allow_all_commands_session:
            return True
        choice = self._approval_choice(
            title="Run command",
            target=comando,
            preview="",
            question="Do you want to run this command?",
            allow_all_label="Yes, allow all commands during this session",
        )
        if choice == "all":
            self.allow_all_commands_session = True
            return True
        return choice == "yes"

    def _create_after_text(self, path: Path, conteudo: str, sobrescrever: bool) -> tuple[str, str] | None:
        if path.exists() and path.is_dir():
            return None
        before = ""
        if path.is_file():
            try:
                before = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                before = ""
        if path.exists() and not sobrescrever:
            return None
        return before, str(conteudo)

    def _edit_after_text(self, path: Path, procurar: str, substituir: str, ocorrencias: int) -> tuple[str, str] | None:
        if not path.is_file() or not procurar:
            return None
        try:
            before = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        count = -1 if ocorrencias <= 0 else ocorrencias
        after = before.replace(procurar, substituir, count)
        if after == before:
            return None
        return before, after

    def _execute_tool_call(self, call: dict[str, Any]) -> dict[str, Any]:
        function_data = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function_data.get("name") or "").strip()
        args = function_data.get("arguments") if isinstance(function_data.get("arguments"), dict) else {}
        caminho = str(args.get("caminho") or "")
        raw_sobrescrever = args.get("sobrescrever", False)
        sobrescrever = raw_sobrescrever if isinstance(raw_sobrescrever, bool) else str(raw_sobrescrever).lower() in {
            "1",
            "true",
            "sim",
            "yes",
        }

        if name == "ler_arquivo":
            result = self.ler_arquivo(caminho)
        elif name == "listar_diretorio":
            result = self.listar_diretorio(caminho)
        elif name == "criar_arquivo":
            path = self._resolve_safe_path(caminho)
            conteudo = str(args.get("conteudo") or "")
            if path is not None:
                texts = self._create_after_text(path, conteudo, sobrescrever)
                if texts and not self._approve_file_edit(action="Edit file" if path.exists() else "Create file", path=path, before=texts[0], after=texts[1]):
                    result = "Erro: edicao recusada pelo usuario."
                    return {
                        "type": "tool_response",
                        "tool_call_id": str(call.get("id") or ""),
                        "role": "tool",
                        "name": name,
                        "content": result,
                    }
            result = self.criar_arquivo(
                caminho,
                conteudo,
                sobrescrever,
            )
        elif name == "editar_arquivo":
            raw_ocorrencias = args.get("ocorrencias", 1)
            try:
                ocorrencias = int(raw_ocorrencias)
            except (TypeError, ValueError):
                ocorrencias = 1
            path = self._resolve_safe_path(caminho)
            if path is not None:
                texts = self._edit_after_text(
                    path,
                    str(args.get("procurar") or ""),
                    str(args.get("substituir") or ""),
                    ocorrencias,
                )
                if texts and not self._approve_file_edit(action="Edit file", path=path, before=texts[0], after=texts[1]):
                    result = "Erro: edicao recusada pelo usuario."
                    return {
                        "type": "tool_response",
                        "tool_call_id": str(call.get("id") or ""),
                        "role": "tool",
                        "name": name,
                        "content": result,
                    }
            result = self.editar_arquivo(
                caminho,
                str(args.get("procurar") or ""),
                str(args.get("substituir") or ""),
                ocorrencias,
            )
        elif name == "executar_comando":
            comando = str(args.get("comando") or "")
            destructive = self._looks_destructive_command(comando)
            allow_destructive = False
            if destructive and not self._allow_destructive_commands():
                if not self._approve_command(comando):
                    result = "Erro: comando recusado pelo usuario."
                    return {
                        "type": "tool_response",
                        "tool_call_id": str(call.get("id") or ""),
                        "role": "tool",
                        "name": name,
                        "content": result,
                    }
                allow_destructive = True
            result = self.executar_comando(comando, allow_destructive=allow_destructive)
        else:
            result = f"Erro: ferramenta desconhecida: {name}"

        return {
            "type": "tool_response",
            "tool_call_id": str(call.get("id") or ""),
            "role": "tool",
            "name": name,
            "content": result,
        }

    def call_llm(self, prompt: str) -> str:
        if not self._use_local_json_tools():
            return self.llm_provider.generate(prompt)

        tool_responses: list[dict[str, Any]] = []
        final_output = ""
        for _ in range(5):
            output = self.llm_provider.generate(self._tool_prompt(prompt, tool_responses))
            final_output = output
            tool_calls = self._parse_tool_calls(output)
            if not tool_calls:
                break

            for tool_call in tool_calls:
                response = self._execute_tool_call(tool_call)
                tool_responses.append(response)
                self._log(f"[{self.name}] Tool {response['name']} executed for call {response['tool_call_id']}")
        else:
            final_output = self.llm_provider.generate(
                self._tool_prompt(prompt, tool_responses)
                + "\n\nLimite de chamadas de ferramentas atingido. "
                "Continue a tarefa original e responda agora com o que ja foi coletado, sem tool_calls."
            )

        return final_output

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

    def _project_paused(self, con: sqlite3.Connection) -> bool:
        row = con.execute("SELECT status_global FROM projeto WHERE id=1").fetchone()
        if not row:
            return False
        status = str(row["status_global"] or "").strip().lower()
        return status == "pausado"

    def _worker_context_mode(self) -> str:
        raw = os.environ.get("MATTED_WORKER_CONTEXT_MODE", DEFAULT_WORKER_CONTEXT_MODE).strip().lower()
        if raw in {"compact", "normal", "adaptive"}:
            return raw
        return DEFAULT_WORKER_CONTEXT_MODE

    def _is_complex_task_text(self, text: str) -> bool:
        lowered = text.strip().lower()
        if len(lowered) > 260:
            return True
        tokens = (
            "refator",
            "arquitet",
            "migr",
            "seguran",
            "vulner",
            "schema",
            "banco",
            "api",
            "teste",
            "ci",
            "pipeline",
            "performance",
            "otimiz",
            "debug",
            "erro",
            "integra",
        )
        return any(token in lowered for token in tokens)

    def _compact_history(self, historico: list[dict[str, Any]], max_items: int = 8, max_msg_chars: int = 280) -> list[dict[str, Any]]:
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

    def _history_for_worker(self, context_text: str, historico: list[dict[str, Any]]) -> list[dict[str, Any]]:
        mode = self._worker_context_mode()
        if mode == "normal":
            return historico
        if mode == "compact":
            return self._compact_history(historico)
        if self._is_complex_task_text(context_text):
            return historico
        return self._compact_history(historico)

    def _build_prompt(self, task: sqlite3.Row, projeto: dict[str, Any], historico: list[dict[str, Any]]) -> str:
        status_global = projeto.get("status_global", "desconhecido")
        effective_history = self._history_for_worker(str(task["solicitacao"]), historico)
        return (
            f"SYSTEM:\n{self.system_prompt}\n\n"
            f"IMPORTANTE: Voce esta operando diretamente dentro da raiz do projeto ({self.root_dir}). "
            "NAO adicione o prefixo 'workspace/' aos caminhos dos arquivos. "
            "Referencie arquivos e diretorios a partir da raiz atual (ex: 'app/main.py', 'schema.sql').\n\n"
            "Se precisar executar uma acao destrutiva, sobrescrever arquivos, instalar dependencias, "
            "ou pedir permissao humana, responda claramente com uma pergunta de aprovacao antes de prosseguir.\n\n"
            "Use as ferramentas nativas disponiveis para ler, editar e criar arquivos quando necessario. "
            "Ao final, responda apenas com o resumo objetivo do que foi feito.\n\n"
            f"STATUS_ATUAL_DO_PROJETO:\n{status_global}\n\n"
            f"PROJETO_COMPLETO:\n{json.dumps(projeto, ensure_ascii=True)}\n\n"
            f"HISTORICO_ULTIMAS_{self.history_tail}:\n{json.dumps(effective_history, ensure_ascii=True)}\n\n"
            f"SOLICITACAO_DA_TAREFA:\n{task['solicitacao']}\n"
        )

    def _build_interactive_prompt(self, user_prompt: str, projeto: dict[str, Any], historico: list[dict[str, Any]]) -> str:
        effective_history = self._history_for_worker(user_prompt, historico)
        return (
            f"SYSTEM:\n{self.system_prompt}\n\n"
            "Voce esta conversando diretamente com o usuario na sua pane tmux.\n"
            f"IMPORTANTE: Voce esta operando diretamente dentro da raiz do projeto ({self.root_dir}). "
            "NAO adicione o prefixo 'workspace/' aos caminhos dos arquivos. "
            "Referencie arquivos e diretorios a partir da raiz atual (ex: 'app/main.py', 'schema.sql').\n"
            "Use as ferramentas nativas disponiveis para ler, editar e criar arquivos quando necessario.\n"
            "Responda sempre somente JSON valido, sem markdown, no formato:\n"
            '{"resposta_usuario": "texto para mostrar ao usuario", "novas_tarefas": []}\n\n'
            "Regras:\n"
            "- resposta_usuario e obrigatoria e deve conter a resposta conversacional para o usuario.\n"
            "- novas_tarefas e opcional; use [] quando nao precisar pedir ajuda a outro agente.\n"
            "- Para pedir ajuda, use novas_tarefas como lista de objetos com agente_destino e solicitacao.\n"
            "- Nao inclua texto fora do JSON.\n\n"
            f"AGENTE_ATUAL:\n{self.name}\n\n"
            f"PROJETO:\n{json.dumps(projeto, ensure_ascii=True)}\n\n"
            f"HISTORICO_ULTIMAS_{self.history_tail}:\n{json.dumps(effective_history, ensure_ascii=True)}\n\n"
            f"MENSAGEM_DO_USUARIO:\n{user_prompt}\n"
        )

    def _claim_oldest_task(self, con: sqlite3.Connection) -> sqlite3.Row | None:
        # Claim atomico compativel com SQLite < 3.35: seleciona e atualiza
        # dentro de uma transacao curta para evitar dois agentes pegarem a mesma tarefa.
        con.execute("BEGIN IMMEDIATE")
        try:
            pending = con.execute(
                """
                SELECT id
                FROM tarefas
                WHERE agente_destino=? AND status='pendente'
                ORDER BY data_criacao ASC, id ASC
                LIMIT 1
                """,
                (self.name,),
            ).fetchone()

            if pending is None:
                con.execute("COMMIT")
                return None

            task_id = int(pending["id"])
            cur = con.execute(
                """
                UPDATE tarefas
                SET status='processando'
                WHERE id=? AND agente_destino=? AND status='pendente'
                """,
                (task_id, self.name),
            )
            if cur.rowcount != 1:
                con.execute("ROLLBACK")
                return None

            row = con.execute(
                """
                SELECT id, agente_destino, status, solicitacao, resposta, data_criacao
                FROM tarefas
                WHERE id=?
                """,
                (task_id,),
            ).fetchone()
            con.execute("COMMIT")
            return row
        except Exception:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise

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
                if self._project_paused(con):
                    time.sleep(self.poll_interval_s)
                    continue

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
                    print(f"\nVocê (para {color_text(self.name, self.name)}): ", end="", flush=True)
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
    ap.add_argument("--system-prompt-file", default="", help="Path to file containing the system prompt (overrides --system-prompt if set)")
    ap.add_argument("--root", default=".")
    ap.add_argument("--poll", type=float, default=0.5, help="Polling interval in seconds")
    ap.add_argument("--history-tail", type=int, default=20, help="How many history rows to include")
    args = ap.parse_args()

    system_prompt = args.system_prompt
    if args.system_prompt_file:
        try:
            system_prompt = Path(args.system_prompt_file).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            print(f"[agent] WARNING: failed to read system prompt file {args.system_prompt_file}: {exc}", flush=True)
            system_prompt = args.system_prompt

    agent = WorkerAgent(
        name=args.name,
        system_prompt=system_prompt,
        root=Path(args.root),
        poll_interval_s=args.poll,
        history_tail=args.history_tail,
    )
    agent.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
