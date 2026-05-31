from pathlib import Path
import sqlite3

from master import MasterOrchestrator


def make_master(tmp_path: Path) -> MasterOrchestrator:
    return MasterOrchestrator(root=tmp_path, agents=["backend_api", "arquiteto_projetos"])


def test_parse_continuation_to_existing_agent(tmp_path):
    master = make_master(tmp_path)

    decision = master._parse_continuation_decision(
        '{"continue": true, "agent": "arquiteto_projetos", "spawn": false, '
        '"skill_prompt": "", "solicitacao": "Revise a entrega do backend.", "motivo": "Revisao cruzada"}'
    )

    assert decision == {
        "continue": True,
        "agent": "arquiteto_projetos",
        "spawn": False,
        "skill_prompt": "",
        "solicitacao": "Revise a entrega do backend.",
        "motivo": "Revisao cruzada",
    }


def test_parse_continuation_rejects_missing_agent_without_spawn(tmp_path):
    master = make_master(tmp_path)

    decision = master._parse_continuation_decision(
        '{"continue": true, "agent": "product_owner", "spawn": false, '
        '"skill_prompt": "", "solicitacao": "Priorize o backlog.", "motivo": "Precisa de PO"}'
    )

    assert decision == {
        "continue": False,
        "agent": "",
        "spawn": False,
        "skill_prompt": "",
        "solicitacao": "",
        "motivo": "Planner escolheu agente inexistente 'product_owner' com spawn=false.",
    }


def test_parse_continuation_can_spawn_coordination_agent(tmp_path):
    master = make_master(tmp_path)

    decision = master._parse_continuation_decision(
        '{"continue": true, "agent": "product_owner", "spawn": true, '
        '"skill_prompt": "Voce e PO e prioriza backlog.", '
        '"solicitacao": "Revise a entrega e defina a proxima prioridade.", "motivo": "Precisa de priorizacao"}'
    )

    assert decision["agent"] == "product_owner"
    assert decision["spawn"] is True
    assert decision["skill_prompt"] == "Voce e PO e prioriza backlog."


def _sample_history(size: int = 12):
    return [
        {"id": i, "autor": "user", "mensagem": f"mensagem {i} " + ("x" * 400), "timestamp": i}
        for i in range(1, size + 1)
    ]


def test_router_history_mode_normal_returns_full_history(tmp_path, monkeypatch):
    master = make_master(tmp_path)
    monkeypatch.setenv("MATTED_ROUTER_CONTEXT_MODE", "normal")
    hist = _sample_history()

    selected = master._history_for_router("oi", hist)

    assert selected == hist


def test_router_history_mode_compact_returns_compacted_history(tmp_path, monkeypatch):
    master = make_master(tmp_path)
    monkeypatch.setenv("MATTED_ROUTER_CONTEXT_MODE", "compact")
    hist = _sample_history()

    selected = master._history_for_router("oi", hist)

    assert len(selected) == 8
    assert selected[0]["id"] == 5
    assert len(selected[-1]["mensagem"]) == 280


def test_router_history_mode_adaptive_uses_full_for_complex_prompt(tmp_path, monkeypatch):
    master = make_master(tmp_path)
    monkeypatch.setenv("MATTED_ROUTER_CONTEXT_MODE", "adaptive")
    hist = _sample_history()

    selected = master._history_for_router("faça refatoração de arquitetura e segurança da API", hist)

    assert selected == hist


def test_router_history_mode_adaptive_uses_compact_for_simple_prompt(tmp_path, monkeypatch):
    master = make_master(tmp_path)
    monkeypatch.setenv("MATTED_ROUTER_CONTEXT_MODE", "adaptive")
    hist = _sample_history()

    selected = master._history_for_router("oi", hist)

    assert len(selected) == 8


def _prepare_tasks_db(tmp_path: Path) -> None:
    con = sqlite3.connect(tmp_path / "squad.db")
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tarefas (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              agente_destino TEXT NOT NULL,
              status TEXT NOT NULL,
              solicitacao TEXT NOT NULL,
              resposta TEXT,
              data_criacao INTEGER NOT NULL,
              master_tratada INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        con.execute(
            "INSERT INTO tarefas (agente_destino, status, solicitacao, resposta, data_criacao, master_tratada) VALUES (?,?,?,?,?,?)",
            ("backend_api", "concluido", "x", "", 1, 1),
        )
        con.execute(
            "INSERT INTO tarefas (agente_destino, status, solicitacao, resposta, data_criacao, master_tratada) VALUES (?,?,?,?,?,?)",
            ("codebase_analyzer", "pendente", "y", "", 2, 0),
        )
        con.commit()
    finally:
        con.close()


def _prepare_history_db(tmp_path: Path) -> None:
    con = sqlite3.connect(tmp_path / "squad.db")
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS historico (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              autor TEXT NOT NULL,
              mensagem TEXT NOT NULL,
              timestamp INTEGER NOT NULL
            );
            """
        )
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            (
                "master",
                "Spawn/chat: agente 'analise_projetos_existentes' preparado com skill: "
                "Voce analisa projetos legados.\n\n"
                "Skill reference: https://example.com/skills/codebase/SKILL.md",
                1,
            ),
        )
        con.commit()
    finally:
        con.close()


def test_sync_agents_from_db_loads_agents_from_tasks(tmp_path):
    _prepare_tasks_db(tmp_path)
    master = MasterOrchestrator(root=tmp_path, agents=[])

    master._sync_agents_from_db()

    assert master.agents == ["backend_api", "codebase_analyzer"]


def test_sync_agents_from_db_does_not_duplicate_existing_agents(tmp_path):
    _prepare_tasks_db(tmp_path)
    master = MasterOrchestrator(root=tmp_path, agents=["backend_api"])

    master._sync_agents_from_db()

    assert master.agents == ["backend_api", "codebase_analyzer"]


def test_sync_agents_from_db_ignores_agents_closed_in_history(tmp_path):
    _prepare_tasks_db(tmp_path)
    con = sqlite3.connect(tmp_path / "squad.db")
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS historico (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              autor TEXT NOT NULL,
              mensagem TEXT NOT NULL,
              timestamp INTEGER NOT NULL
            );
            """
        )
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            ("master", "Agente encerrado por comando do usuário: codebase_analyzer", 10),
        )
        con.commit()
    finally:
        con.close()
    master = MasterOrchestrator(root=tmp_path, agents=[])

    master._sync_agents_from_db()

    assert master.agents == ["backend_api"]


def test_sync_agents_from_db_reloads_agent_spawned_after_close(tmp_path):
    _prepare_tasks_db(tmp_path)
    con = sqlite3.connect(tmp_path / "squad.db")
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS historico (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              autor TEXT NOT NULL,
              mensagem TEXT NOT NULL,
              timestamp INTEGER NOT NULL
            );
            """
        )
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            ("master", "Agente encerrado por comando do usuário: codebase_analyzer", 10),
        )
        con.execute(
            "INSERT INTO historico (autor, mensagem, timestamp) VALUES (?,?,?)",
            ("master", "Spawn/chat: agente 'codebase_analyzer' preparado com skill: nova skill", 11),
        )
        con.commit()
    finally:
        con.close()
    master = MasterOrchestrator(root=tmp_path, agents=[])

    master._sync_agents_from_db()

    assert master.agents == ["backend_api", "codebase_analyzer"]


def test_load_agent_skill_prompts_from_history_preserves_original_reference(tmp_path):
    _prepare_history_db(tmp_path)
    master = MasterOrchestrator(root=tmp_path, agents=[])

    prompts = master._load_agent_skill_prompts_from_history()

    assert "analise_projetos_existentes" in prompts
    assert "Voce analisa projetos legados." in prompts["analise_projetos_existentes"]
    assert "https://example.com/skills/codebase/SKILL.md" in prompts["analise_projetos_existentes"]


def test_restore_uses_history_prompt_without_agent_name_skill_lookup(tmp_path, monkeypatch):
    _prepare_history_db(tmp_path)
    master = MasterOrchestrator(root=tmp_path, agents=["analise_projetos_existentes"])
    spawned = []

    monkeypatch.setattr(master, "_agent_process_running", lambda name: False)
    monkeypatch.setattr(
        master,
        "criar_novo_agente",
        lambda name, prompt, resolve_agent_name_as_skill=True: spawned.append(
            (name, prompt, resolve_agent_name_as_skill)
        ),
    )

    master.restaurar_agentes_ativos()

    assert spawned == [
        (
            "analise_projetos_existentes",
            "Voce analisa projetos legados.\n\nSkill reference: https://example.com/skills/codebase/SKILL.md",
            False,
        )
    ]
