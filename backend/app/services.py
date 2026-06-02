from __future__ import annotations

from datetime import datetime, timezone
from typing import List
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models, schemas
from .repositories import AgentRepository, EventRepository, MessageRepository, ProjectRepository, TaskRepository


PROJECT_STATUSES = {"planning", "active", "paused", "completed", "archived"}
AGENT_STATUSES = {"available", "busy", "paused", "disabled"}
AUTHOR_TYPES = {"user", "master", "agent", "system"}
TASK_STATUSES = {"queued", "assigned", "running", "blocked", "completed", "failed", "cancelled"}
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}

TASK_TRANSITIONS = {
    "queued": {"assigned", "cancelled"},
    "assigned": {"running", "cancelled"},
    "running": {"blocked", "completed", "failed", "cancelled"},
    "blocked": {"running", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


class DomainError(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    status_code = 404


class ConflictError(DomainError):
    status_code = 409


class ValidationError(DomainError):
    status_code = 422


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _flush_or_conflict(session: Session, message: str) -> None:
    try:
        session.flush()
    except IntegrityError as exc:
        raise ConflictError(message) from exc


def _ensure_project(session: Session, project_id: UUID) -> models.Project:
    project = ProjectRepository(session).get(project_id)
    if project is None:
        raise NotFoundError("project not found")
    return project


def _add_event(
    session: Session,
    *,
    project_id: UUID,
    event_type: str,
    summary: str,
    payload: dict,
    task_id: UUID = None,
    agent_id: UUID = None,
) -> models.HistoryEvent:
    event = models.HistoryEvent(
        project_id=project_id,
        task_id=task_id,
        agent_id=agent_id,
        event_type=event_type,
        summary=summary,
        payload=payload,
    )
    EventRepository(session).add(event)
    return event


def list_projects(session: Session) -> List[models.Project]:
    return ProjectRepository(session).list()


def create_project(session: Session, payload: schemas.ProjectCreate) -> models.Project:
    if payload.status not in PROJECT_STATUSES:
        raise ValidationError("invalid project status")
    project = models.Project(
        name=payload.name,
        description=payload.description,
        status=payload.status,
        settings=payload.settings,
    )
    ProjectRepository(session).add(project)
    _flush_or_conflict(session, "project could not be created")
    session.add(
        models.ProjectStatusHistory(
            project_id=project.id,
            previous_status=None,
            new_status=project.status,
            reason="project created",
            changed_by="system",
        )
    )
    _add_event(
        session,
        project_id=project.id,
        event_type="project.created",
        summary="Project created",
        payload={"name": project.name, "status": project.status},
    )
    session.commit()
    session.refresh(project)
    return project


def get_project(session: Session, project_id: UUID) -> models.Project:
    return _ensure_project(session, project_id)


def list_agents(session: Session, project_id: UUID) -> List[models.Agent]:
    _ensure_project(session, project_id)
    return AgentRepository(session).list_for_project(project_id)


def create_agent(session: Session, project_id: UUID, payload: schemas.AgentCreate) -> models.Agent:
    _ensure_project(session, project_id)
    if payload.status not in AGENT_STATUSES:
        raise ValidationError("invalid agent status")
    repo = AgentRepository(session)
    if repo.get_by_slug(project_id, payload.slug) is not None:
        raise ConflictError("agent slug already exists in project")
    agent = models.Agent(
        project_id=project_id,
        slug=payload.slug,
        display_name=payload.display_name,
        role=payload.role,
        system_prompt=payload.system_prompt,
        status=payload.status,
        agent_metadata=payload.metadata,
    )
    for capability in payload.capabilities:
        agent.capabilities.append(models.AgentCapability(capability=capability))
    repo.add(agent)
    _flush_or_conflict(session, "agent could not be created")
    _add_event(
        session,
        project_id=project_id,
        agent_id=agent.id,
        event_type="agent.created",
        summary="Agent created",
        payload={"slug": agent.slug, "role": agent.role, "capabilities": payload.capabilities},
    )
    session.commit()
    session.refresh(agent)
    return agent


def list_tasks(session: Session, project_id: UUID) -> List[models.Task]:
    _ensure_project(session, project_id)
    return TaskRepository(session).list_for_project(project_id)


def create_task(session: Session, project_id: UUID, payload: schemas.TaskCreate) -> models.Task:
    _ensure_project(session, project_id)
    if payload.priority < 0:
        raise ValidationError("priority must be greater than or equal to zero")
    if payload.assigned_agent_id:
        agent = AgentRepository(session).get(payload.assigned_agent_id)
        if agent is None or agent.project_id != project_id:
            raise ValidationError("assigned agent does not belong to project")
    status = "assigned" if payload.assigned_agent_id else "queued"
    task = models.Task(
        project_id=project_id,
        assigned_agent_id=payload.assigned_agent_id,
        title=payload.title,
        description=payload.description,
        status=status,
        priority=payload.priority,
        input_payload=payload.input_payload,
    )
    repo = TaskRepository(session)
    repo.add(task)
    _flush_or_conflict(session, "task could not be created")

    dependencies = repo.get_many_for_project(project_id, payload.depends_on_task_ids)
    if len(dependencies) != len(set(payload.depends_on_task_ids)):
        raise ValidationError("one or more task dependencies were not found in project")
    for dep in dependencies:
        session.add(models.TaskDependency(project_id=project_id, task_id=task.id, depends_on_task_id=dep.id))

    _add_event(
        session,
        project_id=project_id,
        task_id=task.id,
        agent_id=payload.assigned_agent_id,
        event_type="task.created",
        summary="Task created",
        payload={"title": task.title, "status": task.status, "priority": task.priority},
    )
    session.commit()
    session.refresh(task)
    return task


def patch_task_status(session: Session, task_id: UUID, payload: schemas.TaskStatusPatch) -> models.Task:
    if payload.status not in TASK_STATUSES:
        raise ValidationError("invalid task status")
    task = TaskRepository(session).get(task_id)
    if task is None:
        raise NotFoundError("task not found")
    allowed = TASK_TRANSITIONS[task.status]
    if payload.status != task.status and payload.status not in allowed:
        raise ValidationError("invalid task status transition")

    previous = task.status
    task.status = payload.status
    if payload.result_payload is not None:
        task.result_payload = payload.result_payload
    if payload.error_message is not None:
        task.error_message = payload.error_message
    if payload.status == "running" and task.started_at is None:
        task.started_at = _now()
    if payload.status in TERMINAL_TASK_STATUSES and task.completed_at is None:
        task.completed_at = _now()

    _add_event(
        session,
        project_id=task.project_id,
        task_id=task.id,
        agent_id=task.assigned_agent_id,
        event_type="task.status_changed",
        summary="Task status changed",
        payload={"previous_status": previous, "new_status": task.status},
    )
    session.commit()
    session.refresh(task)
    return task


def list_messages(session: Session, project_id: UUID) -> List[models.Message]:
    _ensure_project(session, project_id)
    return MessageRepository(session).list_for_project(project_id)


def create_message(session: Session, project_id: UUID, payload: schemas.MessageCreate) -> models.Message:
    _ensure_project(session, project_id)
    if payload.author_type not in AUTHOR_TYPES:
        raise ValidationError("invalid author type")
    if payload.task_id:
        task = TaskRepository(session).get(payload.task_id)
        if task is None or task.project_id != project_id:
            raise ValidationError("task does not belong to project")
    if payload.agent_id:
        agent = AgentRepository(session).get(payload.agent_id)
        if agent is None or agent.project_id != project_id:
            raise ValidationError("agent does not belong to project")

    message = models.Message(
        project_id=project_id,
        task_id=payload.task_id,
        agent_id=payload.agent_id,
        author_type=payload.author_type,
        author_name=payload.author_name,
        content=payload.content,
        message_metadata=payload.metadata,
    )
    MessageRepository(session).add(message)
    _flush_or_conflict(session, "message could not be created")
    _add_event(
        session,
        project_id=project_id,
        task_id=payload.task_id,
        agent_id=payload.agent_id,
        event_type="message.created",
        summary="Message created",
        payload={"author_type": payload.author_type, "author_name": payload.author_name},
    )
    session.commit()
    session.refresh(message)
    return message


def list_events(session: Session, project_id: UUID) -> List[models.HistoryEvent]:
    _ensure_project(session, project_id)
    return EventRepository(session).list_for_project(project_id)
