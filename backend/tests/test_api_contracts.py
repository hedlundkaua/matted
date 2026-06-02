from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_contract():
    client = TestClient(create_app())
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_project_task_message_flow_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + str(tmp_path / "test.db"))
    monkeypatch.setenv("AUTO_CREATE_TABLES", "1")

    client = TestClient(create_app())

    project_response = client.post("/projects", json={"name": "Orquestrador"})
    assert project_response.status_code == 201
    project_id = project_response.json()["id"]

    agent_response = client.post(
        "/projects/" + project_id + "/agents",
        json={
            "slug": "backend_core",
            "display_name": "Backend Core",
            "role": "API Designer",
            "capabilities": ["fastapi", "postgresql"],
        },
    )
    assert agent_response.status_code == 201
    agent_id = agent_response.json()["id"]

    task_response = client.post(
        "/projects/" + project_id + "/tasks",
        json={"title": "Criar health-check", "assigned_agent_id": agent_id},
    )
    assert task_response.status_code == 201
    task = task_response.json()
    assert task["status"] == "assigned"

    running_response = client.patch("/tasks/" + task["id"] + "/status", json={"status": "running"})
    assert running_response.status_code == 200
    assert running_response.json()["status"] == "running"

    message_response = client.post(
        "/projects/" + project_id + "/messages",
        json={"author_type": "agent", "author_name": "backend_core", "content": "Health-check criado."},
    )
    assert message_response.status_code == 201

    events_response = client.get("/projects/" + project_id + "/events")
    assert events_response.status_code == 200
    assert len(events_response.json()) >= 4
