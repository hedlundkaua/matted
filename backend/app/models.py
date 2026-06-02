from __future__ import annotations

import uuid

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import relationship
from sqlalchemy.types import CHAR, JSON, TypeDecorator

from .db import Base


class GUID(TypeDecorator):
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(postgresql.UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def uuid_pk():
    return Column(GUID(), primary_key=True, default=uuid.uuid4)


def json_dict():
    return Column(MutableDict.as_mutable(JSON), nullable=False, default=dict)


class Project(Base):
    __tablename__ = "projects"

    id = uuid_pk()
    name = Column(Text, nullable=False)
    description = Column(Text)
    status = Column(String(32), nullable=False, default="planning")
    settings = json_dict()
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    archived_at = Column(DateTime(timezone=True))

    agents = relationship("Agent", back_populates="project", cascade="all, delete-orphan")
    tasks = relationship("Task", back_populates="project", cascade="all, delete-orphan")
    messages = relationship("Message", back_populates="project", cascade="all, delete-orphan")
    events = relationship("HistoryEvent", back_populates="project", cascade="all, delete-orphan")
    artifacts = relationship("Artifact", back_populates="project", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("status IN ('planning', 'active', 'paused', 'completed', 'archived')", name="projects_status_check"),
    )


class ProjectStatusHistory(Base):
    __tablename__ = "project_status_history"

    id = uuid_pk()
    project_id = Column(GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    previous_status = Column(String(32))
    new_status = Column(String(32), nullable=False)
    reason = Column(Text)
    changed_by = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "previous_status IS NULL OR previous_status IN ('planning', 'active', 'paused', 'completed', 'archived')",
            name="project_status_history_previous_status_check",
        ),
        CheckConstraint(
            "new_status IN ('planning', 'active', 'paused', 'completed', 'archived')",
            name="project_status_history_new_status_check",
        ),
        Index("idx_project_status_history_project_created", "project_id", "created_at"),
    )


class Agent(Base):
    __tablename__ = "agents"

    id = uuid_pk()
    project_id = Column(GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    slug = Column(String(120), nullable=False)
    display_name = Column(Text, nullable=False)
    role = Column(Text, nullable=False)
    system_prompt = Column(Text)
    status = Column(String(32), nullable=False, default="available")
    agent_metadata = Column("metadata", MutableDict.as_mutable(JSON), nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    project = relationship("Project", back_populates="agents")
    tasks = relationship("Task", back_populates="assigned_agent")
    messages = relationship("Message", back_populates="agent")
    events = relationship("HistoryEvent", back_populates="agent")
    artifacts = relationship("Artifact", back_populates="agent")
    capabilities = relationship("AgentCapability", back_populates="agent", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("status IN ('available', 'busy', 'paused', 'disabled')", name="agents_status_check"),
        UniqueConstraint("project_id", "slug", name="agents_project_slug_unique"),
        Index("idx_agents_project_status", "project_id", "status"),
    )


class AgentCapability(Base):
    __tablename__ = "agent_capabilities"

    id = uuid_pk()
    agent_id = Column(GUID(), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False)
    capability = Column(Text, nullable=False)
    description = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    agent = relationship("Agent", back_populates="capabilities")

    __table_args__ = (
        UniqueConstraint("agent_id", "capability", name="agent_capabilities_unique"),
        Index("idx_agent_capabilities_capability", "capability"),
    )


class Task(Base):
    __tablename__ = "tasks"

    id = uuid_pk()
    project_id = Column(GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    assigned_agent_id = Column(GUID(), ForeignKey("agents.id", ondelete="SET NULL"))
    title = Column(Text, nullable=False)
    description = Column(Text)
    status = Column(String(32), nullable=False, default="queued")
    priority = Column(Integer, nullable=False, default=100)
    input_payload = json_dict()
    result_payload = Column(MutableDict.as_mutable(JSON))
    error_message = Column(Text)
    due_at = Column(DateTime(timezone=True))
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    project = relationship("Project", back_populates="tasks")
    assigned_agent = relationship("Agent", back_populates="tasks")
    messages = relationship("Message", back_populates="task")
    events = relationship("HistoryEvent", back_populates="task")
    artifacts = relationship("Artifact", back_populates="task")

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'assigned', 'running', 'blocked', 'completed', 'failed', 'cancelled')",
            name="tasks_status_check",
        ),
        CheckConstraint("priority >= 0", name="tasks_priority_check"),
        UniqueConstraint("project_id", "id", name="tasks_project_id_id_unique"),
        Index("idx_tasks_project_status_priority", "project_id", "status", "priority", "created_at"),
        Index("idx_tasks_assigned_agent_status", "assigned_agent_id", "status"),
    )


class TaskDependency(Base):
    __tablename__ = "task_dependencies"

    project_id = Column(GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    task_id = Column(GUID(), primary_key=True)
    depends_on_task_id = Column(GUID(), primary_key=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint("task_id <> depends_on_task_id", name="task_dependencies_no_self_reference"),
        ForeignKeyConstraint(["project_id", "task_id"], ["tasks.project_id", "tasks.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["project_id", "depends_on_task_id"], ["tasks.project_id", "tasks.id"], ondelete="CASCADE"),
        Index("idx_task_dependencies_depends_on", "depends_on_task_id"),
    )


class Message(Base):
    __tablename__ = "messages"

    id = uuid_pk()
    project_id = Column(GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    task_id = Column(GUID(), ForeignKey("tasks.id", ondelete="SET NULL"))
    agent_id = Column(GUID(), ForeignKey("agents.id", ondelete="SET NULL"))
    author_type = Column(String(32), nullable=False)
    author_name = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    message_metadata = Column("metadata", MutableDict.as_mutable(JSON), nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    project = relationship("Project", back_populates="messages")
    task = relationship("Task", back_populates="messages")
    agent = relationship("Agent", back_populates="messages")

    __table_args__ = (
        CheckConstraint("author_type IN ('user', 'master', 'agent', 'system')", name="messages_author_type_check"),
        Index("idx_messages_project_created", "project_id", "created_at"),
        Index("idx_messages_task_created", "task_id", "created_at"),
    )


class HistoryEvent(Base):
    __tablename__ = "history_events"

    id = uuid_pk()
    project_id = Column(GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    task_id = Column(GUID(), ForeignKey("tasks.id", ondelete="SET NULL"))
    agent_id = Column(GUID(), ForeignKey("agents.id", ondelete="SET NULL"))
    event_type = Column(Text, nullable=False)
    summary = Column(Text)
    payload = json_dict()
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    project = relationship("Project", back_populates="events")
    task = relationship("Task", back_populates="events")
    agent = relationship("Agent", back_populates="events")

    __table_args__ = (
        Index("idx_history_events_project_created", "project_id", "created_at"),
        Index("idx_history_events_task_created", "task_id", "created_at"),
        Index("idx_history_events_event_type", "event_type"),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id = uuid_pk()
    project_id = Column(GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    task_id = Column(GUID(), ForeignKey("tasks.id", ondelete="SET NULL"))
    agent_id = Column(GUID(), ForeignKey("agents.id", ondelete="SET NULL"))
    name = Column(Text, nullable=False)
    artifact_type = Column(String(32), nullable=False)
    mime_type = Column(Text)
    path = Column(Text)
    content = Column(Text)
    checksum_sha256 = Column(Text)
    artifact_metadata = Column("metadata", MutableDict.as_mutable(JSON), nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    project = relationship("Project", back_populates="artifacts")
    task = relationship("Task", back_populates="artifacts")
    agent = relationship("Agent", back_populates="artifacts")

    __table_args__ = (
        CheckConstraint("artifact_type IN ('document', 'schema', 'code', 'log', 'report', 'other')", name="artifacts_type_check"),
        CheckConstraint("path IS NOT NULL OR content IS NOT NULL", name="artifacts_location_check"),
        Index("idx_artifacts_project_created", "project_id", "created_at"),
        Index("idx_artifacts_task_created", "task_id", "created_at"),
    )
