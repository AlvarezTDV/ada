"""Offline jobs API checks with a gated, interruptible browser stand-in."""

from contextlib import ExitStack, contextmanager
from importlib.util import find_spec
import json
from threading import Event
import unittest

from fastapi.testclient import TestClient

from ada_core import CapabilityKind, ExtensionManifest, ExtensionRegistry, SessionManager, SessionResult
from ada_core.errors import SideEffectProfile
from ada_core.registry import _interruptible_resource
from ada_server.app import create_app


TOKEN = "offline-test-token-not-a-secret"
URL = "https://example.com"


class FakeSession:
    def __init__(self):
        self.messages = []
        self.entered, self.release, self.closed = Event(), Event(), Event()
        self.calls = self.stops = 0
        self.closed_at_shutdown = False
        self.registry = ExtensionRegistry()
        self.registry.register(ExtensionManifest(
            "test.navigation", CapabilityKind.NAVIGATION, SideEffectProfile.PURE_READ,
        ), object)

    @contextmanager
    def browser(self):
        def stop():
            self.stops += 1
            self.release.set()

        try:
            yield None, stop
        finally:
            self.closed.set()

    def handle_text(self, text, *, on_reasoning_notice=None):
        self.calls += 1
        if text.endswith("fail"):
            return SessionResult(f"Provider rejected {TOKEN}", kind="error")
        if not text.lower().startswith("/navegar "):
            return SessionResult(text)

        def navigate(_):
            with _interruptible_resource(self.browser()):
                if on_reasoning_notice:
                    on_reasoning_notice("Reading page")
                self.entered.set()
                self.release.wait(2)
                return SessionResult("Summary", kind="navigation", url=URL)

        return self.registry.execute(CapabilityKind.NAVIGATION, navigate)

    def shutdown(self):
        self.closed_at_shutdown = self.closed.is_set()


class JobsSSETests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        manager = SessionManager(lambda: self.session)
        self.lifespan = self.enterContext(ExitStack())
        self.client = self.lifespan.enter_context(TestClient(
            create_app(manager, lambda: SessionResult("OK"), token=TOKEN),
            headers={"Authorization": f"Bearer {TOKEN}"},
        ))
        self.addCleanup(self.session.release.set)
        session_id = self.client.post("/sessions").json()["session_id"]
        self.chat = f"/sessions/{session_id}/chat"

    def start(self, text=f"/navegar {URL} resume"):
        reply = self.client.post(self.chat, json={"text": text})
        self.assertEqual(reply.status_code, 202, reply.text)
        payload = reply.json()
        self.assertEqual(payload, {"kind": "job", "job_id": payload["job_id"], "job_kind": "navigation"})
        return f"/jobs/{payload['job_id']}"

    def events(self, path, after=None):
        headers = {} if after is None else {"Last-Event-ID": str(after)}
        response = self.client.get(f"{path}/events", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        return [
            (int(seq[4:]), name[7:], json.loads(data[6:]))
            for frame in response.text.strip().split("\n\n")
            if frame and not frame.startswith(":")
            for seq, name, data in [frame.splitlines()]
        ]

    def test_accepts_before_runner_returns(self):
        job = self.start()
        self.assertTrue(self.session.entered.wait(2))
        self.assertFalse(self.session.closed.is_set())
        self.assertEqual(self.client.get(job).json()["state"], "running")

    def test_streams_notice_and_result(self):
        job = self.start(f"/NaVeGaR {URL} resume")
        self.session.release.set()
        events = self.events(job)
        self.assertEqual([name for _, name, _ in events],
                         ["job.started", "job.notice", "job.completed"])
        self.assertEqual(events[1][2], {"text": "Reading page"})
        result = events[-1][2]["result"]
        self.assertEqual((result["text"], result["url"]), ("Summary", URL))
        self.assertEqual(self.client.get(job).json()["result"], result)

    def test_replays_only_later_events(self):
        job = self.start()
        self.session.release.set()
        events = self.events(job)
        self.assertEqual(self.events(job, events[0][0]), events[1:])
        self.assertEqual(self.events(job, events[-1][0]), [])

    def test_conflict_reports_active_job(self):
        job = self.start()
        response = self.client.post(self.chat, json={"text": f"/navegar {URL} resume again"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["job_id"], job.rsplit("/", 1)[1])
        self.assertTrue(self.session.entered.wait(2))
        self.assertEqual(self.session.calls, 1)

    def test_cancel_stops_browser_and_is_idempotent(self):
        job = self.start()
        self.assertTrue(self.session.entered.wait(2))
        for _ in range(2):
            response = self.client.post(f"{job}/cancel")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["cancel_requested"])
        self.assertEqual(self.events(job)[-1][1], "job.cancelled")
        self.assertTrue(self.session.closed.is_set())
        snapshot = self.client.get(job).json()
        self.assertEqual((snapshot["state"], snapshot["result"], self.session.stops), ("cancelled", None, 1))

    def test_rejects_bad_arguments_before_creating_job(self):
        for text in ("/navegar", "/navegar http://example.com resume", f"/navegar {URL} {'x' * 4001}"):
            response = self.client.post(self.chat, json={"text": text})
            self.assertEqual((response.status_code, response.json()["kind"]), (200, "error"))
            self.assertNotIn("job_id", response.json())
        self.assertEqual(self.session.calls, 0)

    def test_shutdown_cancels_before_closing_sessions(self):
        self.start()
        self.assertTrue(self.session.entered.wait(2))
        self.lifespan.close()
        self.assertEqual(self.session.stops, 1)
        self.assertTrue(self.session.closed_at_shutdown)

    def test_protects_job_routes_and_redacts_errors(self):
        job = self.start(f"/navegar {URL} fail")
        for method, suffix in (("GET", ""), ("GET", "/events"), ("POST", "/cancel")):
            response = self.client.request(method, job + suffix, headers={"Authorization": ""})
            self.assertEqual(response.status_code, 401)
        _, name, data = self.events(job)[-1]
        self.assertEqual(name, "job.failed")
        self.assertEqual(data["error"], "Provider rejected [REDACTED]")
        self.assertEqual(self.client.get(job).json()["error"], data["error"])

    def test_non_navigation_chat_stays_synchronous(self):
        response = self.client.post(self.chat, json={"text": "/navegarfoo"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "/navegarfoo")


@unittest.skipUnless(find_spec("uvicorn"), "The offline CI dependency set omits Uvicorn")
class ShutdownWithSSETests(unittest.TestCase):
    def test_graceful_shutdown_interrupts_job_while_sse_is_connected(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import Mock, patch

        from uvicorn import Server

        from ada_server.app import main

        async def check():
            app = create_app(SessionManager(object), lambda: SessionResult("OK"), token=TOKEN)

            def mark_started(server):
                server.started = True

            with (patch("ada_server.app.create_app", return_value=app),
                  patch.dict("os.environ", ADA_TLS_CERTFILE="", ADA_TLS_KEYFILE=""),
                  patch.object(Server, "run", autospec=True, side_effect=mark_started) as run):
                main()
            server = run.call_args.args[0]
            server.config.load()
            server.servers = []
            server.lifespan = server.config.lifespan_class(server.config)
            await server.lifespan.startup()
            stopped, release = Event(), Event()
            disconnect, started = asyncio.Event(), asyncio.Event()

            def stop():
                stopped.set()
                release.set()

            job = app.state.jobs.start_job("navigation", lambda _: release.wait(10), cancel_callback=stop)
            path = f"/jobs/{job.job_id}/events"
            scope = dict(type="http", asgi={"version": "3.0", "spec_version": "2.3"},
                         method="GET", scheme="http", path=path, raw_path=path.encode(),
                         root_path="", query_string=b"", http_version="1.1",
                         headers=[(b"authorization", f"Bearer {TOKEN}".encode())])

            async def receive():
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                if b"event: job.started\n" in message.get("body", b""):
                    started.set()

            protocol_class = server.config.http_protocol_class
            protocol = Mock(spec=protocol_class)
            protocol.cycle = SimpleNamespace(response_complete=False, keep_alive=True)
            protocol.shutdown = lambda: protocol_class.shutdown(protocol)
            server.server_state.connections.add(protocol)
            stream = asyncio.create_task(app(scope, receive, send))
            server.server_state.tasks.add(stream)

            def cleanup(_):
                server.server_state.connections.discard(protocol)
                server.server_state.tasks.discard(stream)

            stream.add_done_callback(cleanup)
            shutdown = None
            try:
                await asyncio.wait_for(started.wait(), 2)
                shutdown = asyncio.create_task(server.shutdown())
                self.assertTrue(await asyncio.to_thread(stopped.wait, 3),
                                "Connected SSE prevented shutdown from interrupting the job")
                await asyncio.wait_for(asyncio.shield(shutdown), 3)
                self.assertEqual(app.state.jobs.snapshot(job.job_id)["state"], "cancelled")
            finally:
                disconnect.set()
                release.set()
                await asyncio.wait_for(shutdown if shutdown is not None else server.shutdown(), 5)

        asyncio.run(check())
