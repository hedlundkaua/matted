from __future__ import annotations

from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import schemas, services
from ..config import get_settings
from ..db import get_session
from ..repositories import ArtifactRepository
from .serializers import (
    agent_to_dict,
    artifact_to_dict,
    event_to_dict,
    message_to_dict,
    project_to_dict,
    task_to_dict,
)


router = APIRouter()


def _handle_domain_error(exc: services.DomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("/health", response_model=schemas.HealthResponse)
def health() -> schemas.HealthResponse:
    settings = get_settings()
    return schemas.HealthResponse(status="ok", app=settings.app_name, environment=settings.environment)


@router.get("/projects", response_model=List[schemas.ProjectRead])
def list_projects(session: Session = Depends(get_session)):
    return [project_to_dict(project) for project in services.list_projects(session)]


@router.post("/projects", response_model=schemas.ProjectRead, status_code=201)
def create_project(payload: schemas.ProjectCreate, session: Session = Depends(get_session)):
    try:
        return project_to_dict(services.create_project(session, payload))
    except services.DomainError as exc:
        raise _handle_domain_error(exc)


@router.get("/projects/{project_id}", response_model=schemas.ProjectRead)
def get_project(project_id: UUID, session: Session = Depends(get_session)):
    try:
        return project_to_dict(services.get_project(session, project_id))
    except services.DomainError as exc:
        raise _handle_domain_error(exc)


@router.get("/projects/{project_id}/agents", response_model=List[schemas.AgentRead])
def list_agents(project_id: UUID, session: Session = Depends(get_session)):
    try:
        return [agent_to_dict(agent) for agent in services.list_agents(session, project_id)]
    except services.DomainError as exc:
        raise _handle_domain_error(exc)


@router.post("/projects/{project_id}/agents", response_model=schemas.AgentRead, status_code=201)
def create_agent(project_id: UUID, payload: schemas.AgentCreate, session: Session = Depends(get_session)):
    try:
        return agent_to_dict(services.create_agent(session, project_id, payload))
    except services.DomainError as exc:
        raise _handle_domain_error(exc)


@router.get("/projects/{project_id}/tasks", response_model=List[schemas.TaskRead])
def list_tasks(project_id: UUID, session: Session = Depends(get_session)):
    try:
        return [task_to_dict(task) for task in services.list_tasks(session, project_id)]
    except services.DomainError as exc:
        raise _handle_domain_error(exc)


@router.post("/projects/{project_id}/tasks", response_model=schemas.TaskRead, status_code=201)
def create_task(project_id: UUID, payload: schemas.TaskCreate, session: Session = Depends(get_session)):
    try:
        return task_to_dict(services.create_task(session, project_id, payload))
    except services.DomainError as exc:
        raise _handle_domain_error(exc)


@router.patch("/tasks/{task_id}/status", response_model=schemas.TaskRead)
def patch_task_status(task_id: UUID, payload: schemas.TaskStatusPatch, session: Session = Depends(get_session)):
    try:
        return task_to_dict(services.patch_task_status(session, task_id, payload))
    except services.DomainError as exc:
        raise _handle_domain_error(exc)


@router.get("/projects/{project_id}/messages", response_model=List[schemas.MessageRead])
def list_messages(project_id: UUID, session: Session = Depends(get_session)):
    try:
        return [message_to_dict(message) for message in services.list_messages(session, project_id)]
    except services.DomainError as exc:
        raise _handle_domain_error(exc)


@router.post("/projects/{project_id}/messages", response_model=schemas.MessageRead, status_code=201)
def create_message(project_id: UUID, payload: schemas.MessageCreate, session: Session = Depends(get_session)):
    try:
        return message_to_dict(services.create_message(session, project_id, payload))
    except services.DomainError as exc:
        raise _handle_domain_error(exc)


@router.get("/projects/{project_id}/events", response_model=List[schemas.EventRead])
def list_events(project_id: UUID, session: Session = Depends(get_session)):
    try:
        return [event_to_dict(event) for event in services.list_events(session, project_id)]
    except services.DomainError as exc:
        raise _handle_domain_error(exc)


@router.get("/projects/{project_id}/artifacts", response_model=List[schemas.ArtifactRead])
def list_artifacts(project_id: UUID, session: Session = Depends(get_session)):
    try:
        services.get_project(session, project_id)
        return [artifact_to_dict(artifact) for artifact in ArtifactRepository(session).list_for_project(project_id)]
    except services.DomainError as exc:
        raise _handle_domain_error(exc)
