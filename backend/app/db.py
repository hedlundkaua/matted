from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from .config import get_settings


Base = declarative_base()


def build_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite:///") and not database_url.startswith("sqlite:///:memory:"):
        db_path = Path(database_url[len("sqlite:///") :])
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, future=True, pool_pre_ping=True, connect_args=connect_args)


@lru_cache(maxsize=8)
def _get_engine(database_url: str) -> Engine:
    return build_engine(database_url)


def get_engine(database_url: str = None) -> Engine:
    return _get_engine(database_url or get_settings().database_url)


def build_session_factory(database_url: str = None):
    return sessionmaker(bind=get_engine(database_url), class_=Session, expire_on_commit=False, autoflush=False, future=True)


def init_db() -> None:
    # Import models so SQLAlchemy registers metadata before create_all.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=get_engine())


def get_session() -> Generator[Session, None, None]:
    session = build_session_factory()()
    try:
        yield session
    finally:
        session.close()
