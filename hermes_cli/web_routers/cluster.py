"""Dashboard endpoints for selecting the active Mongo fleet agent."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class ClusterActivateRequest(BaseModel):
    target: str
    reason: Optional[str] = "dashboard"


def _storage():
    from hermes_storage import get_storage, is_mongo_mode

    if not is_mongo_mode():
        raise HTTPException(status_code=409, detail="Mongo cluster mode is not enabled")
    return get_storage(force=True)


@router.get("/api/cluster")
async def cluster_status():
    """Return the active node, handoff state, and online fleet members."""
    return _storage().cluster_status()


@router.post("/api/cluster/activate")
async def activate_cluster_node(body: ClusterActivateRequest):
    """Request a safe messaging/session handoff to an online node."""
    try:
        state = _storage().activate(body.target, reason=body.reason or "dashboard")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "state": state}
