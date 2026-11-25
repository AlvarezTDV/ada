"""Offline HTTP contract checks; no provider or network sockets."""

import asyncio
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from ada_core.domain import SessionMode, SessionSummary
from ada_core.orchestrator import SessionResult
from ada_server.app import create_app


TOKEN = "offline-test-token-not-a-secret"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'")


def assert_worker_thread():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise AssertionError("Blocking core operation ran on the event loop")


class FakeSessionManager:
    def __init__(self):
        self.sessions = {}
        self.next_id = 0

    def create_session(self, mode, title):
        self.next_id += 1
        session_id = str(self.next_id)
        summary = SessionSummary(session_id, mode, title, "created", "updated", 1)
        self.sessions[session_id] = summary
        return summary

    def list_sessions(self):
        return tuple(self.sessions.values())

    def delete_session(self, session_id):
        return self.sessions.pop(session_id, None) is not None

    def handle_text(self, session_id, text):
        assert_worker_thread()
        summary = self.sessions[session_id]
        if text == "salir":
            if summary.mode is SessionMode.EPHEMERAL:
                self.delete_session(session_id)
            return SessionResult("Goodbye", kind="exit")
        return SessionResult(text, notices=("fake notice",))

    def shutdown_all(self):
        pass


def fake_status():
    assert_worker_thread()
    return SessionResult(f"fake-model: {TOKEN}", kind="status")


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.manager = FakeSessionManager()
        self.client = self.enterContext(TestClient(
            create_app(self.manager, fake_status, token=TOKEN, cookie_ttl=60),
            base_url="https://127.0.0.1",
        ))
        self.bearer = {"Authorization": f"Bearer {TOKEN}"}

    def test_public_health_and_query_token_rejection(self):
        response = self.client.get("/health")
        self.assertEqual((response.status_code, response.json()), (200, {"status": "ok"}))
        self.assertEqual(self.client.get("/health?token=").status_code, 400)
        self.assertEqual(self.client.post("/auth/login?token=x", json={"token": TOKEN}).status_code, 400)

    def test_protected_routes_require_auth_and_reject_query_tokens(self):
        routes = (("GET", "/auth/me"), ("POST", "/auth/logout"), ("POST", "/sessions"),
                  ("GET", "/sessions"), ("DELETE", "/sessions/missing"),
                  ("POST", "/sessions/missing/chat"), ("GET", "/status"))
        for method, path in routes:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.client.request(method, path).status_code, 401)
                response = self.client.request(method, f"{path}?token={TOKEN}", headers=self.bearer)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn(TOKEN, response.text)
        self.assertEqual(self.client.post("/sessions", content="{").status_code, 401)
        self.assertEqual(self.manager.sessions, {})

    def test_login_cookie_chat_and_logout(self):
        self.assertEqual(self.client.post("/auth/login", json={"token": "wrong"}).status_code, 401)
        invalid = self.client.post("/auth/login", json={"token": [TOKEN]})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn(TOKEN, invalid.text)
        login = self.client.post("/auth/login", json={"token": TOKEN})
        self.assertEqual(login.status_code, 200)
        cookie = login.headers["set-cookie"]
        for flag in ("ada_session=", "HttpOnly", "Secure", "SameSite=strict", "Path=/", "Max-Age=60"):
            self.assertIn(flag.lower(), cookie.lower())
        self.assertNotIn(TOKEN, cookie)
        self.assertEqual(self.client.get("/auth/me").json(), {"authenticated": True})
        created = self.client.post("/sessions")
        self.assertEqual(created.status_code, 201)
        chat = self.client.post(f"/sessions/{created.json()['session_id']}/chat", json={"text": "hello"})
        self.assertEqual((chat.status_code, chat.json()), (200, {
            "kind": "chat", "text": "hello", "mode": "casual", "url": "", "notices": ["fake notice"],
        }))
        logout = self.client.post("/auth/logout")
        self.assertEqual(logout.status_code, 204)
        self.assertIn("Max-Age=0", logout.headers["set-cookie"])
        self.assertEqual(self.client.get("/auth/me").status_code, 401)

    def test_signed_cookie_expires_and_rejects_tampering(self):
        with patch("ada_server.auth.time.time", return_value=10):
            self.client.post("/auth/login", json={"token": TOKEN})
            signed = self.client.cookies.get("ada_session")
            self.assertEqual(self.client.get("/auth/me").status_code, 200)
            self.client.cookies.clear()
            self.assertEqual(self.client.get("/auth/me", headers={"Cookie": f"ada_session=x{signed}"}).status_code, 401)
        with patch("ada_server.auth.time.time", return_value=70):
            self.assertEqual(self.client.get("/auth/me", headers={"Cookie": f"ada_session={signed}"}).status_code, 401)

    def test_bearer_status_redaction_and_security_headers(self):
        status = self.client.get("/status", headers=self.bearer)
        self.assertEqual((status.status_code, status.json()["text"]), (200, "fake-model: [REDACTED]"))
        denied = self.client.get("/status", headers={"Authorization": "Bearer invalid"})
        self.assertEqual(denied.status_code, 401)
        for response in (self.client.get("/health"), status, denied, self.client.get("/status?token=x")):
            for name, value in {"Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
                                "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                "Cache-Control": "no-store"}.items():
                self.assertEqual(response.headers[name], value)

    def test_status_redacts_raw_and_escaped_injected_secret(self):
        secret = 'mock-marker-"quoted\\section-400'

        def status():
            return SessionResult(f"raw={secret}; json={json.dumps(secret)}; repr={secret!r}", kind="status")

        with TestClient(create_app(FakeSessionManager(), status, token=secret),
                        base_url="https://127.0.0.1") as client:
            self.assertEqual(client.post("/auth/login", json={"token": secret}).status_code, 200)
            response = client.get("/status")
            self.assertEqual((response.status_code, response.json()["text"]),
                             (200, """raw=[REDACTED]; json="[REDACTED]"; repr='[REDACTED]'"""))

    def test_token_rotation_invalidates_old_credentials(self):
        self.assertEqual(self.client.post("/auth/login", json={"token": TOKEN}).status_code, 200)
        cookie = self.client.cookies["ada_session"]
        rotated = "offline-test-token-after-rotation"
        with TestClient(create_app(FakeSessionManager(), fake_status, token=rotated),
                        base_url="https://127.0.0.1") as client:
            response = client.get("/auth/me", headers={"Cookie": f"ada_session={cookie}"})
            self.assertEqual((response.status_code, response.json()),
                             (401, {"detail": "Authentication required"}))
            self.assertEqual(client.get("/auth/me", headers=self.bearer).status_code, 401)
            self.assertEqual(client.get("/auth/me", headers={"Authorization": f"Bearer {rotated}"}).status_code, 200)

    def test_exit_then_list_create_and_delete(self):
        self.client.headers.update(self.bearer)
        created = self.client.post("/sessions", json={"mode": "ephemeral", "title": "One"})
        summary = created.json()
        self.assertEqual(self.client.get("/sessions").json(), [summary])
        path = f"/sessions/{summary['session_id']}/chat"
        self.assertEqual(self.client.post(path, json={"text": "salir"}).json()["kind"], "exit")
        self.assertEqual(self.client.post(path, json={"text": "again"}).status_code, 404)
        self.assertEqual(self.client.get("/sessions").json(), [])
        replacement = self.client.post("/sessions", json={"mode": "persistent"})
        self.assertEqual(replacement.status_code, 201)
        self.assertEqual(replacement.json()["mode"], "persistent")
        target = f"/sessions/{replacement.json()['session_id']}"
        self.assertEqual(self.client.delete(target).status_code, 204)
        self.assertEqual(self.client.delete(target).status_code, 404)
        self.assertEqual(self.client.get("/sessions").json(), [])

    def test_missing_empty_and_short_tokens_fail_closed(self):
        with patch.dict("os.environ", {"ADA_TOKEN": ""}):
            for token in (None, "", "short", " " * 16):
                with self.subTest(token=token), self.assertRaisesRegex(ValueError, "ADA_TOKEN"):
                    create_app(self.manager, fake_status, token=token)
