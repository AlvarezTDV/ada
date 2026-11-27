"""Authenticated job snapshots, SSE replay, and cancellation."""

import json

from anyio.to_thread import run_sync
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse


router = APIRouter(prefix="/jobs")


def get_jobs(request, job_id):
    jobs = request.app.state.jobs
    try:
        jobs.get_job(job_id)
    except KeyError:
        raise HTTPException(404, "Job not found") from None
    return jobs


def redact_error(data, redact):
    error = data.get("error")
    return data | {"error": redact(error)} if isinstance(error, str) else data


@router.get("/{job_id}")
async def snapshot(job_id: str, request: Request):
    return redact_error(get_jobs(request, job_id).snapshot(job_id), request.app.state.auth.redact)


@router.get("/{job_id}/events")
async def events(job_id: str, request: Request):
    jobs = get_jobs(request, job_id)
    redact = request.app.state.auth.redact
    try:
        since = int(request.headers.get("Last-Event-ID") or "0")
        if since < 0:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Invalid Last-Event-ID") from None

    async def frames():
        stream = jobs.iter_events(job_id, since_seq=since)
        end = object()
        while True:
            item = await run_sync(next, stream, end, limiter=request.app.state.sse_limiter)
            if item is end:
                break
            if item is None:
                yield ": ping\n\n"
            else:
                data = json.dumps(redact_error(item.data, redact))
                yield f"id: {item.seq}\nevent: {item.event}\ndata: {data}\n\n"

    return StreamingResponse(frames(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no"})


@router.post("/{job_id}/cancel")
async def cancel(job_id: str, request: Request):
    jobs = get_jobs(request, job_id)
    await run_sync(jobs.cancel_job, job_id)
    return redact_error(jobs.snapshot(job_id), request.app.state.auth.redact)
