from __future__ import annotations

def project_to_dict(project):
    return {
        "id": project.id,
        "name": project.name,
        "description": project.description,
        "status": project.status,
        "settings": project.settings or {},
        "created_at": project.created_at,
        "updated_at": project.updated_at,
    }


def agent_to_dict(agent):
    return {
        "id": agent.id,
        "project_id": agent.project_id,
        "slug": agent.slug,
        "display_name": agent.display_name,
        "role": agent.role,
        "system_prompt": agent.system_prompt,
        "status": agent.status,
        "metadata": agent.agent_metadata or {},
        "capabilities": [cap.capability for cap in agent.capabilities],
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


def task_to_dict(task):
    return {
        "id": task.id,
        "project_id": task.project_id,
        "assigned_agent_id": task.assigned_agent_id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "input_payload": task.input_payload or {},
        "result_payload": task.result_payload,
        "error_message": task.error_message,
        "due_at": task.due_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def message_to_dict(message):
    return {
        "id": message.id,
        "project_id": message.project_id,
        "task_id": message.task_id,
        "agent_id": message.agent_id,
        "author_type": message.author_type,
        "author_name": message.author_name,
        "content": message.content,
        "metadata": message.message_metadata or {},
        "created_at": message.created_at,
    }


def event_to_dict(event):
    return {
        "id": event.id,
        "project_id": event.project_id,
        "task_id": event.task_id,
        "agent_id": event.agent_id,
        "event_type": event.event_type,
        "summary": event.summary,
        "payload": event.payload or {},
        "created_at": event.created_at,
    }


def artifact_to_dict(artifact):
    return {
        "id": artifact.id,
        "project_id": artifact.project_id,
        "task_id": artifact.task_id,
        "agent_id": artifact.agent_id,
        "name": artifact.name,
        "artifact_type": artifact.artifact_type,
        "mime_type": artifact.mime_type,
        "path": artifact.path,
        "content": artifact.content,
        "checksum_sha256": artifact.checksum_sha256,
        "metadata": artifact.artifact_metadata or {},
        "created_at": artifact.created_at,
        "updated_at": artifact.updated_at,
    }
