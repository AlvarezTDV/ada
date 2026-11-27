"""Translate HTTP requests into calls to the session manager."""

from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated

from anyio.to_thread import run_sync
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from ada_core.domain import SessionSummary
from ada_core.jobs import JobBusy, JobFailed
from ada_core.orchestrator import SessionResult
from ada_core.session_manager import SessionManager
from .schemas import ChatRequest, ChatResult, CreateSession, JobAccepted


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


@router.post("/sessions/{session_id}/chat", response_model=ChatResult,
             responses={202: {"model": JobAccepted}, 409: {"description": "Heavy job already active"}})
async def chat(session_id: str, body: ChatRequest, manager: Manager, request: Request):
    words = body.text.split(maxsplit=2)
    if words and words[0].lower() == "/navegar":
        if not any(session.session_id == session_id for session in await run_sync(manager.list_sessions)):
            raise HTTPException(404, "Session not found")
        return start_navigation(request, manager, session_id, body.text, words)
    try:
        return await run_sync(manager.handle_text, session_id, body.text)
    except KeyError:
        raise HTTPException(404, "Session not found") from None


def start_navigation(request, manager, session_id, text, words):
    if len(words) != 3:
        return SessionResult("Usa /navegar <https-url> <tarea de lectura>.", kind="error")
    from browser_navigation import BrowserError, validate_task, validate_url

    try:
        validate_url(words[1])
        validate_task(words[2].strip())
    except BrowserError as error:
        return SessionResult(str(error), kind="error")

    jobs, redact = request.app.state.jobs, request.app.state.auth.redact

    def navigate(job):
        def notice(message):
            jobs.emit(job.job_id, "job.notice", {"text": redact(message)})

        result = manager.handle_text(session_id, text, on_reasoning_notice=notice)
        if result.kind == "error":
            raise JobFailed(redact(result.text))
        return asdict(result) | {"text": redact(result.text), "url": redact(result.url),
                                 "notices": [redact(item) for item in result.notices]}

    try:
        job = jobs.start_job("navigation", navigate)
    except JobBusy as error:
        raise HTTPException(409, {"message": str(error), "job_id": error.job_id}) from None
    return JSONResponse(JobAccepted(job_id=job.job_id, job_kind=job.kind).model_dump(), status_code=202)


@router.get("/status", response_model=ChatResult)
async def status(request: Request, read_status: StatusReader) -> SessionResult:
    result = await run_sync(read_status)
    return SessionResult(request.app.state.auth.redact(result.text), kind="status")
