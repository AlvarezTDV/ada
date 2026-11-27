"""Process-local, single-slot jobs and replayable event streams."""

from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from threading import Condition, Event, Thread
from uuid import uuid4


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_TERMINAL = {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
_cancel_event: ContextVar[Event | None] = ContextVar("job_cancel_event", default=None)


class JobBusy(RuntimeError):
    def __init__(self, job_id: str):
        super().__init__("A heavy job is already active")
        self.job_id = job_id


class JobCancelled(Exception):
    """Unwind a cooperative runner without falling back to another provider."""


class JobFailed(Exception):
    """An expected failure with a user-facing message."""


@dataclass(frozen=True)
class JobEvent:
    seq: int
    event: str
    data: dict


@dataclass
class JobRecord:
    job_id: str
    kind: str
    state: JobState = JobState.QUEUED
    result: dict | None = None
    error: str | None = None
    cancel_event: Event = field(default_factory=Event)
    _events: deque[JobEvent] = field(default_factory=lambda: deque(maxlen=500))
    _seq: int = 0


class JobManager:
    """Runners own their work until they return; cancellation never frees the slot early."""

    def __init__(self):
        self._changed = Condition()
        self._jobs: dict[str, JobRecord] = {}
        self._active: JobRecord | None = None
        self._worker: Thread | None = None
        self._cancel_callback = None
        self._cancelling = False
        self._closed = False

    def start_job(self, kind: str, runner, *, cancel_callback=None) -> JobRecord:
        """Run ``runner(record)`` in a thread; the optional hook requests stop without joining it."""
        with self._changed:
            if self._active is not None:
                raise JobBusy(self._active.job_id)
            if self._closed:
                raise RuntimeError("Job manager is closed")
            job = JobRecord(uuid4().hex, kind)
            self._jobs[job.job_id] = self._active = job
            self._cancel_callback = cancel_callback
            self._worker = Thread(target=self._run, args=(job, runner), name=f"ada-job-{job.job_id}")
            try:
                self._worker.start()
            except RuntimeError:
                del self._jobs[job.job_id]
                self._active = self._worker = self._cancel_callback = None
                raise
            return job

    def _run(self, job, runner):
        state, result, error = JobState.COMPLETED, None, None
        token = _cancel_event.set(job.cancel_event)
        try:
            with self._changed:
                if job.cancel_event.is_set():
                    raise JobCancelled
                job.state = JobState.RUNNING
                self._publish(job, "job.started", {"state": job.state})
            result = runner(job)
        except JobCancelled:
            state = JobState.CANCELLED
        except BaseException as exc:
            state = JobState.FAILED
            error = str(exc) if isinstance(exc, JobFailed) else "Job failed"
        finally:
            _cancel_event.reset(token)
            with self._changed:
                self._changed.wait_for(lambda: not self._cancelling)
                if job.cancel_event.is_set():
                    state, result, error = JobState.CANCELLED, None, None
                job.state, job.result, job.error = state, result, error
                self._active = self._cancel_callback = None
                self._publish(job, f"job.{state.value}", self.snapshot(job.job_id))

    def _publish(self, job, event, data):
        job._seq += 1
        job._events.append(JobEvent(job._seq, event, dict(data)))
        self._changed.notify_all()

    def emit(self, job_id, event, data):
        if not event or "\r" in event or "\n" in event:
            raise ValueError("Invalid event name")
        with self._changed:
            job = self.get_job(job_id)
            if job.state not in _TERMINAL:
                self._publish(job, event, data)

    def set_state(self, job_id, state):
        state = JobState(state)
        if state not in (JobState.RUNNING, JobState.WAITING_APPROVAL):
            raise ValueError("Only the manager can transition to queued or terminal states")
        with self._changed:
            job = self.get_job(job_id)
            if job.state not in _TERMINAL and job.state != state:
                job.state = state
                self._publish(job, "job.state", {"state": state})

    def get_job(self, job_id) -> JobRecord:
        with self._changed:
            return self._jobs[job_id]

    def snapshot(self, job_id) -> dict:
        with self._changed:
            job = self.get_job(job_id)
            return dict(job_id=job.job_id, job_kind=job.kind, state=job.state,
                        result=job.result, error=job.error, cancel_requested=job.cancel_event.is_set())

    def iter_events(self, job_id, *, since_seq=0, heartbeat_s=15.0):
        job = self.get_job(job_id)
        while True:
            with self._changed:
                self._changed.wait_for(
                    lambda: job._seq > since_seq or job.state in _TERMINAL, heartbeat_s,
                )
                events = [event for event in job._events if event.seq > since_seq]
                if not events and job.state in _TERMINAL:
                    return
            if events:
                since_seq = events[-1].seq
                yield from events
            else:
                yield None

    def cancel_job(self, job_id):
        with self._changed:
            job = self.get_job(job_id)
            if job.state in _TERMINAL or job.cancel_event.is_set():
                return
            job.cancel_event.set()
            callback = self._cancel_callback
            self._cancelling = callback is not None
        if callback is not None:
            try:
                callback()
            finally:
                with self._changed:
                    self._cancelling = False
                    self._changed.notify_all()

    def shutdown(self):
        with self._changed:
            self._closed = True
            job, worker = self._active, self._worker
        try:
            if job is not None:
                self.cancel_job(job.job_id)
        finally:
            if worker is not None:
                worker.join()
