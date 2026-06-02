from __future__ import annotations

from fastapi import FastAPI

from .api.routes import router
from .config import get_settings
from .db import init_db


OPENAPI_TAGS = [
    {"name": "health", "description": "Service health and runtime metadata."},
    {"name": "projects", "description": "Project records from the projects table."},
    {"name": "agents", "description": "Project agents and their capabilities."},
    {"name": "tasks", "description": "Queued, assigned, running and completed project tasks."},
    {"name": "messages", "description": "Conversation messages linked to projects, tasks and agents."},
    {"name": "events", "description": "History events emitted by project workflows."},
    {"name": "artifacts", "description": "Generated documents, code, logs, reports and related artifacts."},
]


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.auto_create_tables:
        init_db()

    app = FastAPI(
        title=settings.app_name,
        description="API inicial para orquestracao multiagente baseada no schema do banco.",
        version="0.1.0",
        openapi_tags=OPENAPI_TAGS,
    )
    app.include_router(router)

    @app.on_event("startup")
    def on_startup() -> None:
        if settings.auto_create_tables:
            init_db()

    return app


app = create_app()
