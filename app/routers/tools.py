"""``GET /api/tools``: the job-tool registry for the UI (options schema, limits, availability)."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from app.core import registry

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("")
def list_tools() -> Response:
    """Every registered tool with its options schema, limits and availability (registration order)."""
    return JSONResponse({"tools": [registry.tool_json(t) for t in registry.all_tools()]})


@router.get("/{tool_id}")
def get_tool(tool_id: str) -> Response:
    try:
        tool = registry.get(tool_id)
    except KeyError:
        return JSONResponse({"detail": "Unknown tool"}, status_code=404)
    return JSONResponse(registry.tool_json(tool))
