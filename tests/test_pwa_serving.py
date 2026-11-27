import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

from fastapi.testclient import TestClient

from ada_server.app import create_app


class PwaServingTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(object(), lambda: None, token="test-only-token-1234"))
        self.addCleanup(self.client.close)

    def test_shell_is_public_with_security_headers_and_correct_types(self):
        routes = {
            "/": {"text/html"},
            "/manifest.webmanifest": {"application/manifest+json"},
            "/sw.js": {"text/javascript", "application/javascript"},
            "/static/app.js": {"text/javascript", "application/javascript"},
            "/static/styles.css": {"text/css"},
            "/static/icon.svg": {"image/svg+xml"},
        }
        for path, types in routes.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(response.headers["content-type"].split(";")[0], types)
                for directive in ("script-src 'self'", "style-src 'self'", "connect-src 'self'"):
                    self.assertIn(directive, response.headers["content-security-policy"].split("; "))
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertNotIn("access-control-allow-origin", response.headers)
                if path == "/sw.js":
                    self.assertEqual(response.headers["cache-control"], "no-cache")
                    self.assertEqual(response.headers["service-worker-allowed"], "/")

    def test_public_shell_does_not_open_api_or_accept_query_credentials(self):
        for path in ("/auth/me", "/sessions", "/jobs/missing/events", "/status", "/projects"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.get("/?token=test-only-token-1234").status_code, 400)

    def test_shell_uses_external_assets_and_safe_dom_sinks(self):
        html = self.client.get("/").text
        self.assertRegex(html, r'<script src="/static/app\.js" defer></script>')
        self.assertNotRegex(html, r"(?is)<script\b[^>]*>\s*[^<\s]|\s(?:style|on\w+)\s*=")
        js = self.client.get("/static/app.js").text
        self.assertNotRegex(js, r"\b(?:innerHTML|outerHTML|insertAdjacentHTML|eval|localStorage)\b")

    def test_shell_uses_official_colors(self):
        css = self.client.get("/static/styles.css").text
        for color in ("#fd2636", "#ff0080"):
            self.assertIn(color, css)

    def test_worker_caches_only_the_static_shell(self):
        worker = self.client.get("/sw.js").text
        shell = re.search(r"const SHELL = (\[[^\]]+\]);", worker)
        self.assertIsNotNone(shell)
        self.assertCountEqual(json.loads(shell[1]), [
            "/", "/manifest.webmanifest", "/static/app.js", "/static/styles.css", "/static/icon.svg",
        ])
        self.assertIn("SHELL.includes(url.pathname)", worker)
        self.assertNotRegex(worker, r"\.(?:put|add)\(")


class PwaNavigationTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for the browser contract probe")
    def test_started_event_and_outage_recovery(self):
        source = Path(__file__).resolve().parents[1] / "ada_server" / "static" / "app.js"
        js = r"""
const assert = require("node:assert/strict"), vm = require("node:vm");
const source = require("node:fs").readFileSync(process.argv[1], "utf8");
const probe = process.argv[2], elements = new Map();
const make = () => ({ value: "", children: [], querySelector: make,
  append(...items) { this.children.push(...items); },
  replaceChildren(...items) { this.children = items; this.value = items[0]?.value || ""; },
});
const byId = (id) => {
  if (!elements.has(id)) elements.set(id, make());
  return elements.get(id);
};
let stream, retry;
class EventSource {
  static CLOSED = 2;
  constructor(path) { assert.equal(path, "/jobs/j/events"); this.listeners = {}; stream = this; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  close() { this.readyState = EventSource.CLOSED; }
}
const fetch = async (path) => {
  const [status, data] = {
    "/auth/me": [200, {}], "/sessions": [200, [{ session_id: "s", mode: "ephemeral" }]],
    "/sessions/s/chat": [202, { job_id: "j" }], "/jobs/j": [503, null],
  }[path];
  return { ok: status < 300, status, json: async () => data };
};
const turn = () => new Promise((resolve) => setImmediate(resolve));
(async () => {
  vm.runInNewContext(source, { document: { getElementById: byId, createElement: make },
    EventSource, fetch, navigator: {}, console, URL,
    setTimeout: (fn) => { retry = fn; }, clearTimeout: () => { retry = undefined; },
  });
  await turn();
  byId("message").value = "/navegar https://example.com/ Resume la página";
  byId("chat").onsubmit({ preventDefault() {} });
  await turn();
  assert(stream);
  const started = { lastEventId: "1", data: '{"state":"running"}' };
  stream.listeners["job.started"]?.(started);
  const progressCount = () => byId("messages").children.filter(
    (node) => node.textContent === "Navegación en curso…",
  ).length;
  if (probe === "started") assert.equal(progressCount(), 1);
  else {
    const original = stream;
    stream.readyState = EventSource.CLOSED;
    await stream.onerror();
    assert.equal(byId("cancel").hidden, false, "A temporary 503 must not discard the job");
    assert.equal(byId("send").disabled, true, "The job still occupies the navigation slot");
    retry();
    assert.notEqual(stream, original);
    stream.listeners["job.started"](started);
    assert.equal(progressCount(), 1, "Replayed progress must not appear twice");
    stream.listeners["job.cancelled"]({ lastEventId: "2", data: '{"state":"cancelled"}' });
    assert.equal(byId("cancel").hidden, true);
    assert.equal(byId("send").disabled, false);
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        for probe in ("started", "outage"):
            with self.subTest(probe=probe):
                run = subprocess.run(["node", "-e", js, str(source), probe],
                                     capture_output=True, text=True, timeout=10)
                self.assertEqual(run.returncode, 0, run.stderr)
