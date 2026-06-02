from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


JsonDict = Dict[str, Any]


class HealthResponse(BaseModel):
    status: str
    app: str
    environment: str


class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None
    status: str = "planning"
    settings: JsonDict = Field(default_factory=dict)


class ProjectRead(BaseModel):
    id: UUID
    name: str
    description: Optional[str]
    status: str
    settings: JsonDict
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


class AgentCreate(BaseModel):
    slug: str
    display_name: str
    role: str
    system_prompt: Optional[str] = None
    status: str = "available"
    metadata: JsonDict = Field(default_factory=dict)
    capabilities: List[str] = Field(default_factory=list)


class AgentRead(BaseModel):
    id: UUID
    project_id: UUID
    slug: str
    display_name: str
    role: str
    system_prompt: Optional[str]
    status: str
    metadata: JsonDict
    capabilities: List[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    assigned_agent_id: Optional[UUID] = None
    priority: int = 100
    input_payload: JsonDict = Field(default_factory=dict)
    depends_on_task_ids: List[UUID] = Field(default_factory=list)


class TaskStatusPatch(BaseModel):
    status: str
    result_payload: Optional[JsonDict] = None
    error_message: Optional[str] = None


class TaskRead(BaseModel):
    id: UUID
    project_id: UUID
    assigned_agent_id: Optional[UUID]
    title: str
    description: Optional[str]
    status: str
    priority: int
    input_payload: JsonDict
    result_payload: Optional[JsonDict]
    error_message: Optional[str]
    due_at: Optional[datetime]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


class MessageCreate(BaseModel):
    task_id: Optional[UUID] = None
    agent_id: Optional[UUID] = None
    author_type: str
    author_name: str
    content: str
    metadata: JsonDict = Field(default_factory=dict)


class MessageRead(BaseModel):
    id: UUID
    project_id: UUID
    task_id: Optional[UUID]
    agent_id: Optional[UUID]
    author_type: str
    author_name: str
    content: str
    metadata: JsonDict
    created_at: Optional[datetime]


class EventRead(BaseModel):
    id: UUID
    project_id: UUID
    task_id: Optional[UUID]
    agent_id: Optional[UUID]
    event_type: str
    summary: Optional[str]
    payload: JsonDict
    created_at: Optional[datetime]


class ArtifactRead(BaseModel):
    id: UUID
    project_id: UUID
    task_id: Optional[UUID]
    agent_id: Optional[UUID]
    name: str
    artifact_type: str
    mime_type: Optional[str]
    path: Optional[str]
    content: Optional[str]
    checksum_sha256: Optional[str]
    metadata: JsonDict
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


class ErrorResponse(BaseModel):
    detail: str
