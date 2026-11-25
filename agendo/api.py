"""Core FastAPI server for Agendo (hardware/domain-agnostic)."""

from __future__ import annotations

import uuid
from importlib import resources
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse

from agendo.capture import count_actions, get_action_detail, list_actions
from agendo.restore import ActionNotFoundError, ConflictError, restore_action


def _read_ui_asset(filename: str) -> str:
    return resources.files("agendo").joinpath("ui", filename).read_text(encoding="utf-8")


def create_app() -> FastAPI:
    """Create the core Agendo FastAPI application."""
    application = FastAPI(title="Agendo", version="0.1.0")

    @application.get("/", response_class=HTMLResponse)
    def serve_ui() -> HTMLResponse:
        return HTMLResponse(content=_read_ui_asset("index.html"))

    @application.get("/static/styles.css")
    def serve_styles() -> Response:
        return Response(content=_read_ui_asset("styles.css"), media_type="text/css")

    @application.get("/actions")
    def api_list_actions(
        response: Response,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> list[dict[str, Any]]:
        total = count_actions()
        response.headers["X-Total-Count"] = str(total)
        return list_actions(limit=limit, offset=offset)

    @application.get("/actions/{action_id}")
    def api_get_action(action_id: str) -> dict[str, Any]:
        if action_id != "untracked":
            try:
                uuid.UUID(action_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=f"Invalid action_id UUID: '{action_id}'",
                ) from exc

        detail = get_action_detail(action_id)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"Action {action_id} not found")
        return detail

    @application.post("/actions/{action_id}/undo")
    def api_undo_action(action_id: uuid.UUID) -> JSONResponse:
        try:
            result = restore_action(str(action_id))
            return JSONResponse(status_code=200, content=jsonable_encoder(result))
        except ActionNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConflictError as exc:
            return JSONResponse(status_code=409, content=jsonable_encoder(exc.to_dict()))

    return application


app = create_app()
