"""Checklist plugin tools (REDMINE_CHECKLISTS_ENABLED gated).

Auto-detects the checklist backend on each call:

1. ``redmine_advanced_checklists`` — ``GET /questionlist/{issue_id}``,
   ``PATCH /question/{item_id}``
2. RedmineUP Checklists Pro — ``/issues/{id}/checklists.json``,
   ``PUT /checklists/{id}.json``

The advanced_checklists endpoint is tried first; on HTTP 404 the RedmineUP
endpoint is used instead.
"""

import json
from typing import Any, Dict, List, Optional

from redminelib.exceptions import ResourceNotFoundError, UnknownError

from .._client import _get_redmine_client
from .._env import _is_checklists_enabled, _is_read_only_mode
from .._errors import _READ_ONLY_ERROR, _handle_redmine_error
from .._serialization import wrap_insecure_content
from .._validation import _is_positive_int
from ..server import mcp

# advanced_checklists plugin PATCH actions (see AdvancedChecklistHelper)
_AC_PATCH_COMPLETE = "question.complete"
_AC_PATCH_SET_TITLE = "question.set_title"
_AC_PATCH_SET_ORDER = "question.set_order"

_PROVIDER_ADVANCED = "advanced_checklists"
_PROVIDER_REDMINEUP = "redmineup"


def _checklists_disabled_error() -> Dict[str, str]:
    return {
        "error": (
            "Checklist support is disabled. "
            "Set REDMINE_CHECKLISTS_ENABLED=true to enable it."
        )
    }


def _is_checklist_endpoint_not_found(exc: Exception) -> bool:
    """True when the HTTP error means the plugin route does not exist."""
    if isinstance(exc, ResourceNotFoundError):
        return True
    if isinstance(exc, UnknownError) and exc.status_code == 404:
        return True
    return False


def _fetch_redmineup_checklist_items(issue_id: int) -> List[Dict[str, Any]]:
    """Fetch checklist items from the RedmineUP Checklists endpoint."""
    from .. import _client

    client = _get_redmine_client()
    url = f"{_client.REDMINE_URL}/issues/{issue_id}/checklists.json"
    payload = client.engine.request("get", url)
    raw_items = payload if isinstance(payload, list) else payload.get("checklists", [])
    items = []
    for item in raw_items:
        items.append(
            {
                "id": item.get("id"),
                "subject": wrap_insecure_content(item.get("subject", "")),
                "is_done": item.get("is_done", False),
                "position": item.get("position"),
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            }
        )
    return items


def _serialize_advanced_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """Map an advanced_checklists task to the shared item shape."""
    item: Dict[str, Any] = {
        "id": task.get("id"),
        "subject": wrap_insecure_content(task.get("title", "")),
        "is_done": task.get("done", False),
        "position": task.get("sort_order"),
        "updated_at": str(task.get("updated_at") or ""),
        "questionlist_id": task.get("questionlist_id"),
    }
    if task.get("completed_at"):
        item["completed_at"] = str(task.get("completed_at"))
    if task.get("completed_by"):
        item["completed_by"] = task.get("completed_by")
    assigned = task.get("assigned_to")
    if assigned:
        item["assigned_to"] = {
            "id": assigned.get("id"),
            "type": assigned.get("type"),
            "name": assigned.get("name"),
        }
    if task.get("due_date"):
        item["due_date"] = str(task.get("due_date"))
    if task.get("status"):
        item["status"] = task.get("status")
    return item


def _serialize_advanced_checklist(checklist: Dict[str, Any]) -> Dict[str, Any]:
    tasks = [
        _serialize_advanced_task(task) for task in checklist.get("tasks", [])
    ]
    return {
        "id": checklist.get("id"),
        "title": wrap_insecure_content(checklist.get("title", "")),
        "list_type": checklist.get("list_type"),
        "sort_order": checklist.get("sort_order"),
        "updated_at": str(checklist.get("updated_at") or ""),
        "created_by": checklist.get("created_by"),
        "editable": checklist.get("editable"),
        "tasks": tasks,
        "task_count": len(tasks),
    }


def _looks_like_advanced_checklists_payload(payload: Any) -> bool:
    """True when the JSON matches redmine_advanced_checklists list shape."""
    if not isinstance(payload, list):
        return False
    if not payload:
        return True
    return all(isinstance(item, dict) and "tasks" in item for item in payload)


def _fetch_advanced_checklists(issue_id: int) -> Dict[str, Any]:
    """Fetch checklists from the redmine_advanced_checklists plugin."""
    from .. import _client

    client = _get_redmine_client()
    url = f"{_client.REDMINE_URL}/questionlist/{issue_id}"
    payload = client.engine.request("get", url)
    if not _looks_like_advanced_checklists_payload(payload):
        raise ResourceNotFoundError()
    raw_checklists = payload
    checklists = [
        _serialize_advanced_checklist(checklist) for checklist in raw_checklists
    ]
    items: List[Dict[str, Any]] = []
    for checklist in checklists:
        for task in checklist.get("tasks", []):
            item = dict(task)
            item["checklist_id"] = checklist.get("id")
            item["checklist_title"] = checklist.get("title")
            items.append(item)
    return {"checklists": checklists, "items": items}


def _build_advanced_get_response(
    issue_id: int, data: Dict[str, Any]
) -> Dict[str, Any]:
    return {
        "issue_id": issue_id,
        "provider": _PROVIDER_ADVANCED,
        "checklists": data["checklists"],
        "items": data["items"],
        "total_count": len(data["items"]),
    }


def _build_redmineup_get_response(
    issue_id: int, items: List[Dict[str, Any]]
) -> Dict[str, Any]:
    return {
        "issue_id": issue_id,
        "provider": _PROVIDER_REDMINEUP,
        "total_count": len(items),
        "items": items,
    }


def _fetch_checklist_for_issue(issue_id: int) -> Dict[str, Any]:
    """Try advanced_checklists first, then RedmineUP."""
    try:
        data = _fetch_advanced_checklists(issue_id)
        return _build_advanced_get_response(issue_id, data)
    except Exception as exc:
        if not _is_checklist_endpoint_not_found(exc):
            raise

    items = _fetch_redmineup_checklist_items(issue_id)
    return _build_redmineup_get_response(issue_id, items)


def _update_redmineup_checklist_item(
    checklist_item_id: int, updates: Dict[str, Any]
) -> Any:
    from .. import _client

    client = _get_redmine_client()
    url = f"{_client.REDMINE_URL}/checklists/{checklist_item_id}.json"
    payload = json.dumps({"checklist": updates})
    return client.engine.request(
        "put",
        url,
        headers={"Content-Type": "application/json"},
        data=payload,
    )


def _advanced_patch_item(checklist_item_id: int, action: str, value: Any) -> Any:
    from .. import _client

    client = _get_redmine_client()
    url = f"{_client.REDMINE_URL}/question/{checklist_item_id}"
    payload = json.dumps({"data": {"action": action, "value": value}})
    return client.engine.request(
        "patch",
        url,
        headers={"Content-Type": "application/json"},
        data=payload,
    )


def _update_advanced_checklist_item(
    checklist_item_id: int, updates: Dict[str, Any]
) -> List[str]:
    """Apply advanced_checklists updates (one PATCH per field)."""
    applied: List[str] = []
    if "subject" in updates:
        _advanced_patch_item(
            checklist_item_id, _AC_PATCH_SET_TITLE, updates["subject"]
        )
        applied.append("subject")
    if "is_done" in updates:
        _advanced_patch_item(
            checklist_item_id, _AC_PATCH_COMPLETE, updates["is_done"]
        )
        applied.append("is_done")
    if "position" in updates:
        _advanced_patch_item(
            checklist_item_id, _AC_PATCH_SET_ORDER, updates["position"]
        )
        applied.append("position")
    return applied


def _update_checklist_item_on_server(
    checklist_item_id: int, updates: Dict[str, Any]
) -> tuple[str, List[str]]:
    """Try advanced_checklists first, then RedmineUP."""
    try:
        updated_fields = _update_advanced_checklist_item(checklist_item_id, updates)
        return _PROVIDER_ADVANCED, updated_fields
    except Exception as exc:
        if not _is_checklist_endpoint_not_found(exc):
            raise

    _update_redmineup_checklist_item(checklist_item_id, updates)
    return _PROVIDER_REDMINEUP, list(updates.keys())


# Backward-compatible names used by tests
def _fetch_checklist_items(issue_id: int) -> List[Dict[str, Any]]:
    return _fetch_redmineup_checklist_items(issue_id)


def _update_checklist_item_api(checklist_item_id: int, updates: Dict[str, Any]) -> Any:
    return _update_redmineup_checklist_item(checklist_item_id, updates)


@mcp.tool()
async def get_checklist(issue_id: int) -> Dict[str, Any]:
    """Retrieve checklist data for a Redmine issue.

    Requires ``REDMINE_CHECKLISTS_ENABLED=true``. Automatically detects the
    checklist plugin: tries ``redmine_advanced_checklists`` first
    (``/questionlist/{issue_id}``), then RedmineUP Checklists Pro.

    Args:
        issue_id: The ID of the issue whose checklist to retrieve.

    Returns:
        ``provider`` indicates which plugin answered. For advanced_checklists
        also includes grouped ``checklists``; both backends return flat
        ``items`` with ``id``, ``subject``, ``is_done``, ``position``, and
        ``updated_at``.
    """
    if not _is_checklists_enabled():
        return _checklists_disabled_error()

    if not _is_positive_int(issue_id):
        return {"error": "issue_id must be a positive integer."}

    try:
        return _fetch_checklist_for_issue(issue_id)
    except Exception as e:
        return _handle_redmine_error(
            e,
            f"fetching checklist for issue {issue_id}",
            {"resource_type": "checklist", "resource_id": issue_id},
        )


@mcp.tool()
async def update_checklist_item(
    checklist_item_id: int,
    subject: Optional[str] = None,
    is_done: Optional[bool] = None,
    position: Optional[int] = None,
) -> Dict[str, Any]:
    """Update a checklist item's text, done state, or position.

    Requires ``REDMINE_CHECKLISTS_ENABLED=true``. Tries ``redmine_advanced_checklists``
    first, then RedmineUP. Blocked when ``REDMINE_MCP_READ_ONLY=true``.

    Args:
        checklist_item_id: The ID of the checklist item (task) to update.
        subject: New text for the checklist item (optional).
        is_done: New done state (optional).
        position: New position/order (optional).

    Returns:
        A success dict with the updated fields, or an error dict on failure.
    """
    if _is_read_only_mode():
        return dict(_READ_ONLY_ERROR)

    if not _is_checklists_enabled():
        return _checklists_disabled_error()

    if not _is_positive_int(checklist_item_id):
        return {"error": "checklist_item_id must be a positive integer."}

    updates: Dict[str, Any] = {}
    if subject is not None:
        updates["subject"] = subject
    if is_done is not None:
        if not isinstance(is_done, bool):
            return {"error": "is_done must be a boolean."}
        updates["is_done"] = is_done
    if position is not None:
        if not _is_positive_int(position):
            return {"error": "position must be a positive integer."}
        updates["position"] = position

    if not updates:
        return {
            "error": (
                "No fields to update. Provide at least one of: "
                "subject, is_done, position."
            )
        }

    try:
        provider, updated_fields = _update_checklist_item_on_server(
            checklist_item_id, updates
        )
        return {
            "success": True,
            "checklist_item_id": checklist_item_id,
            "provider": provider,
            "updated_fields": updated_fields,
        }
    except Exception as e:
        return _handle_redmine_error(
            e,
            f"updating checklist item {checklist_item_id}",
            {"resource_type": "checklist_item", "resource_id": checklist_item_id},
        )
