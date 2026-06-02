from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_name: str = "Multi-Agent Orchestrator API"
    environment: str = "development"
    database_url: str = "sqlite:///./workspace/backend_dev.db"
    auto_create_tables: bool = True


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def get_settings() -> Settings:
    return Settings(
        app_name=os.getenv("APP_NAME", Settings.app_name),
        environment=os.getenv("APP_ENV", Settings.environment),
        database_url=os.getenv("DATABASE_URL", Settings.database_url),
        auto_create_tables=_bool_env("AUTO_CREATE_TABLES", Settings.auto_create_tables),
    )
