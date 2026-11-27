"""Translate HTTP requests into calls to the session manager."""

from collections.abc import Callable
from typing import Annotated

from anyio.to_thread import run_sync
from fastapi import APIRouter, Depends, HTTPException, Request, Response

from ada_core.domain import SessionSummary
from ada_core.orchestrator import SessionResult
from ada_core.session_manager import SessionManager
from .schemas import ChatRequest, ChatResult, CreateSession


router = APIRouter()


async def get_manager(request: Request) -> SessionManager:
    return request.app.state.manager


async def get_status(request: Request) -> Callable[[], SessionResult]:
    return request.app.state.status


Manager = Annotated[SessionManager, Depends(get_manager)]
StatusReader = Annotated[Callable[[], SessionResult], Depends(get_status)]


@router.post("/sessions", status_code=201)
async def create_session(manager: Manager, body: CreateSession = CreateSession()) -> SessionSummary:
    return await run_sync(manager.create_session, body.mode, body.title)


@router.get("/sessions")
async def list_sessions(manager: Manager) -> tuple[SessionSummary, ...]:
    return await run_sync(manager.list_sessions)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, manager: Manager) -> Response:
    if not await run_sync(manager.delete_session, session_id):
        raise HTTPException(404, "Session not found")
    return Response(status_code=204)


@router.post("/sessions/{session_id}/chat", response_model=ChatResult)
async def chat(session_id: str, body: ChatRequest, manager: Manager) -> SessionResult:
    try:
        return await run_sync(manager.handle_text, session_id, body.text)
    except KeyError:
        raise HTTPException(404, "Session not found") from None


@router.get("/status", response_model=ChatResult)
async def status(request: Request, read_status: StatusReader) -> SessionResult:
    result = await run_sync(read_status)
    return SessionResult(request.app.state.auth.redact(result.text), kind="status")
