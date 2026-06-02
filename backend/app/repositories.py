from __future__ import annotations

from typing import Iterable, List, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list(self) -> List[models.Project]:
        return list(self.session.scalars(select(models.Project).order_by(models.Project.created_at.desc(), models.Project.id)))

    def get(self, project_id: UUID) -> Optional[models.Project]:
        return self.session.get(models.Project, project_id)

    def add(self, project: models.Project) -> models.Project:
        self.session.add(project)
        return project


class AgentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for_project(self, project_id: UUID) -> List[models.Agent]:
        stmt = select(models.Agent).where(models.Agent.project_id == project_id).order_by(models.Agent.slug)
        return list(self.session.scalars(stmt))

    def get(self, agent_id: UUID) -> Optional[models.Agent]:
        return self.session.get(models.Agent, agent_id)

    def get_by_slug(self, project_id: UUID, slug: str) -> Optional[models.Agent]:
        stmt = select(models.Agent).where(models.Agent.project_id == project_id, models.Agent.slug == slug)
        return self.session.scalars(stmt).first()

    def add(self, agent: models.Agent) -> models.Agent:
        self.session.add(agent)
        return agent


class TaskRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for_project(self, project_id: UUID) -> List[models.Task]:
        stmt = (
            select(models.Task)
            .where(models.Task.project_id == project_id)
            .order_by(models.Task.status, models.Task.priority.asc(), models.Task.created_at.asc())
        )
        return list(self.session.scalars(stmt))

    def get(self, task_id: UUID) -> Optional[models.Task]:
        return self.session.get(models.Task, task_id)

    def get_many_for_project(self, project_id: UUID, task_ids: Iterable[UUID]) -> List[models.Task]:
        ids = list(task_ids)
        if not ids:
            return []
        stmt = select(models.Task).where(models.Task.project_id == project_id, models.Task.id.in_(ids))
        return list(self.session.scalars(stmt))

    def add(self, task: models.Task) -> models.Task:
        self.session.add(task)
        return task


class MessageRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for_project(self, project_id: UUID) -> List[models.Message]:
        stmt = select(models.Message).where(models.Message.project_id == project_id).order_by(models.Message.created_at.asc())
        return list(self.session.scalars(stmt))

    def add(self, message: models.Message) -> models.Message:
        self.session.add(message)
        return message


class EventRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for_project(self, project_id: UUID) -> List[models.HistoryEvent]:
        stmt = (
            select(models.HistoryEvent)
            .where(models.HistoryEvent.project_id == project_id)
            .order_by(models.HistoryEvent.created_at.asc(), models.HistoryEvent.id)
        )
        return list(self.session.scalars(stmt))

    def add(self, event: models.HistoryEvent) -> models.HistoryEvent:
        self.session.add(event)
        return event


class ArtifactRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for_project(self, project_id: UUID) -> List[models.Artifact]:
        stmt = select(models.Artifact).where(models.Artifact.project_id == project_id).order_by(models.Artifact.created_at.asc())
        return list(self.session.scalars(stmt))
