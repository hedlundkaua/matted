from pathlib import Path

from agent_base import WorkerAgent
from llm.codex import CodexProvider
from llm.openrouter import OpenRouterProvider
from terminal_colors import color_text, colorize_bracketed_names


def test_codex_cli_runs_with_workspace_write_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MATTED_CODEX_SANDBOX", raising=False)
    monkeypatch.delenv("MATTED_CODEX_APPROVAL", raising=False)
    provider = CodexProvider(root_dir=str(tmp_path))

    cmd = provider._codex_cli_cmd("last-message.txt")

    assert "--sandbox" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert "--ask-for-approval" in cmd
    assert cmd[cmd.index("--ask-for-approval") + 1] == "on-request"
    assert "--cd" in cmd
    assert Path(cmd[cmd.index("--cd") + 1]) == tmp_path.resolve()
    assert "--output-last-message" in cmd
    assert cmd[cmd.index("--output-last-message") + 1] == "last-message.txt"


def test_codex_cli_output_is_quiet_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MATTED_CODEX_VERBOSE", raising=False)
    provider = CodexProvider(root_dir=str(tmp_path))

    assert provider._verbose_codex_output() is False


def test_legacy_json_tools_are_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MATTED_LEGACY_JSON_TOOLS", raising=False)
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)

    assert agent._use_legacy_json_tools() is False


def test_legacy_json_tools_can_be_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("MATTED_LEGACY_JSON_TOOLS", "1")
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)

    assert agent._use_legacy_json_tools() is True


def test_openrouter_local_tools_are_enabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)
    agent.llm_provider = OpenRouterProvider()

    assert agent._use_local_json_tools() is True


def test_openrouter_local_tools_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setenv("MATTED_OPENROUTER_LOCAL_TOOLS", "0")
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)
    agent.llm_provider = OpenRouterProvider()

    assert agent._use_local_json_tools() is False


def test_codex_does_not_use_local_json_tools_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MATTED_LEGACY_JSON_TOOLS", raising=False)
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)
    agent.llm_provider = CodexProvider(root_dir=str(tmp_path))

    assert agent._use_local_json_tools() is False


def test_editar_arquivo_replaces_exact_text(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("print('old')\n", encoding="utf-8")
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)

    result = agent.editar_arquivo("app.py", "old", "new")

    assert "Arquivo editado com sucesso" in result
    assert target.read_text(encoding="utf-8") == "print('new')\n"


def test_executar_comando_runs_in_project_root(tmp_path):
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)

    result = agent.executar_comando("python3 -c \"import os; print(os.getcwd())\"")

    assert "exit_code=0" in result
    assert str(tmp_path) in result


def test_executar_comando_blocks_destructive_by_default(tmp_path):
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)

    result = agent.executar_comando("rm README.md")

    assert "potencialmente destrutivo bloqueado" in result


def test_tool_edit_requires_approval_and_applies_yes(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)
    monkeypatch.setattr("builtins.input", lambda: "1")

    response = agent._execute_tool_call(
        {
            "id": "call_1",
            "function": {
                "name": "editar_arquivo",
                "arguments": {"caminho": "app.py", "procurar": "old", "substituir": "new"},
            },
        }
    )

    assert "Arquivo editado com sucesso" in response["content"]
    assert target.read_text(encoding="utf-8") == "new\n"


def test_tool_edit_can_be_denied(tmp_path, monkeypatch):
    target = tmp_path / "app.py"
    target.write_text("old\n", encoding="utf-8")
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)
    monkeypatch.setattr("builtins.input", lambda: "3")

    response = agent._execute_tool_call(
        {
            "id": "call_1",
            "function": {
                "name": "editar_arquivo",
                "arguments": {"caminho": "app.py", "procurar": "old", "substituir": "new"},
            },
        }
    )

    assert "recusada pelo usuario" in response["content"]
    assert target.read_text(encoding="utf-8") == "old\n"


def test_tool_edit_allow_all_skips_next_prompt(tmp_path, monkeypatch):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("old\n", encoding="utf-8")
    second.write_text("old\n", encoding="utf-8")
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)
    calls = iter(["2"])
    monkeypatch.setattr("builtins.input", lambda: next(calls))

    for file_name in ("first.py", "second.py"):
        response = agent._execute_tool_call(
            {
                "id": f"call_{file_name}",
                "function": {
                    "name": "editar_arquivo",
                    "arguments": {"caminho": file_name, "procurar": "old", "substituir": "new"},
                },
            }
        )
        assert "Arquivo editado com sucesso" in response["content"]

    assert first.read_text(encoding="utf-8") == "new\n"
    assert second.read_text(encoding="utf-8") == "new\n"


def test_tool_destructive_command_can_be_approved(tmp_path, monkeypatch):
    target = tmp_path / "delete-me.txt"
    target.write_text("x", encoding="utf-8")
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)
    monkeypatch.setattr("builtins.input", lambda: "1")

    response = agent._execute_tool_call(
        {
            "id": "call_1",
            "function": {
                "name": "executar_comando",
                "arguments": {"comando": "rm delete-me.txt"},
            },
        }
    )

    assert "exit_code=0" in response["content"]
    assert not target.exists()


def test_parse_longcat_tool_call_format(tmp_path):
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)

    calls = agent._parse_tool_calls(
        "<longcat_tool_call>ler_arquivo\n"
        "<longcat_arg_key>caminho</longcat_arg_key>\n"
        "<longcat_arg_value>README.md</longcat_arg_value>\n"
        "</longcat_tool_call>"
    )

    assert calls == [
        {
            "id": "longcat_1",
            "type": "function",
            "function": {"name": "ler_arquivo", "arguments": {"caminho": "README.md"}},
        }
    ]


def test_filtered_stream_hides_prompt_echo_and_shows_agent_work(tmp_path):
    provider = CodexProvider(root_dir=str(tmp_path))
    state = {"suppress_prompt": False, "expect_exec_cmd": False}

    assert provider._should_print_codex_line("workdir: /tmp\n", stream_name="stdout", state=state) is False
    assert provider._should_print_codex_line("user\n", stream_name="stdout", state=state) is False
    assert provider._should_print_codex_line("SYSTEM:\n", stream_name="stdout", state=state) is False
    assert provider._should_print_codex_line("codex\n", stream_name="stdout", state=state) is False
    assert provider._should_print_codex_line("Vou editar o arquivo.\n", stream_name="stdout", state=state) is True


def test_filtered_stream_formats_exec_command(tmp_path, monkeypatch):
    monkeypatch.setenv("MATTED_COLOR", "0")
    provider = CodexProvider(root_dir=str(tmp_path))
    state = {"suppress_prompt": False, "expect_exec_cmd": False}

    assert provider._should_print_codex_line("exec\n", stream_name="stdout", state=state) is False
    assert provider._should_print_codex_line("sed -n '1,20p' main.py\n", stream_name="stdout", state=state) is True

    assert provider._format_codex_line("sed -n '1,20p' main.py\n", state, agent_name="backend") == "$ sed -n '1,20p' main.py"


def test_agent_names_are_colorized_distinctly(monkeypatch):
    monkeypatch.setenv("MATTED_COLOR", "1")
    monkeypatch.setenv("TERM", "xterm-256color")

    backend = color_text("backend_api", "[backend_api]")
    architect = color_text("arquiteto_projetos", "[arquiteto_projetos]")

    assert backend != "[backend_api]"
    assert architect != "[arquiteto_projetos]"
    assert backend != architect
    assert colorize_bracketed_names("[backend_api] pronto").endswith(" pronto")


def _sample_history(size: int = 12):
    return [
        {"id": i, "autor": "user", "mensagem": f"mensagem {i} " + ("y" * 400), "timestamp": i}
        for i in range(1, size + 1)
    ]


def test_worker_history_mode_normal_returns_full_history(tmp_path, monkeypatch):
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)
    monkeypatch.setenv("MATTED_WORKER_CONTEXT_MODE", "normal")
    hist = _sample_history()

    selected = agent._history_for_worker("oi", hist)

    assert selected == hist


def test_worker_history_mode_compact_returns_compacted_history(tmp_path, monkeypatch):
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)
    monkeypatch.setenv("MATTED_WORKER_CONTEXT_MODE", "compact")
    hist = _sample_history()

    selected = agent._history_for_worker("oi", hist)

    assert len(selected) == 8
    assert selected[0]["id"] == 5
    assert len(selected[-1]["mensagem"]) == 280


def test_worker_history_mode_adaptive_uses_full_for_complex_text(tmp_path, monkeypatch):
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)
    monkeypatch.setenv("MATTED_WORKER_CONTEXT_MODE", "adaptive")
    hist = _sample_history()

    selected = agent._history_for_worker("corrija vulnerabilidades e refatore arquitetura da API", hist)

    assert selected == hist


def test_worker_history_mode_adaptive_uses_compact_for_simple_text(tmp_path, monkeypatch):
    agent = WorkerAgent(name="backend", system_prompt="test", root=tmp_path)
    monkeypatch.setenv("MATTED_WORKER_CONTEXT_MODE", "adaptive")
    hist = _sample_history()

    selected = agent._history_for_worker("ok", hist)

    assert len(selected) == 8
