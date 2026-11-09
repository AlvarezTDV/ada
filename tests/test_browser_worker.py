import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import browser_worker as worker


@unittest.skipUnless(importlib.util.find_spec("browser_use"), "optional isolated Browser Use environment")
class WorkerConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {"ANONYMIZED_TELEMETRY": "false", "BROWSER_USE_CLOUD_SYNC": "false",
                                      "PYTHON_DOTENV_DISABLED": "1", "ADA_BROWSER_MODEL": "qwen2.5:3b"})
        env.start()
        self.addCleanup(env.stop)

    async def test_only_vetted_read_actions_are_registered(self):
        tools = worker.read_tools("example.com")
        self.assertEqual(set(tools.registry.registry.actions), {"open_page", "read_page", "done"})
        session = Mock(get_current_page=AsyncMock())
        action = tools.registry.registry.actions["open_page"].function
        for url in ("https://127.0.0.1", "https://@example.com/", "https://:@example.com/"):
            with self.subTest(url=url), self.assertRaises(worker.BrowserError):
                await action(url=url, browser_session=session)
        session.get_current_page.assert_not_called()

    async def test_page_observations_are_structured_and_reject_foreign_origins(self):
        tools = worker.read_tools("example.com")
        action = tools.registry.registry.actions["read_page"].function
        for native in (False, True):
            for url in ("https://example.com/page", "https://other.example/page", "http://127.0.0.1",
                        "https://@example.com/", "https://:@example.com/"):
                snapshot = {"url": url, "title": "Observed title", "language": "en", "text": "Observed text"}
                raw = snapshot if native else json.dumps(snapshot)
                page = Mock(evaluate=AsyncMock(return_value=raw), goto=AsyncMock())
                session = Mock(get_current_page=AsyncMock(return_value=page))
                with self.subTest(url=url, native=native):
                    if url.startswith("https://example.com/"):
                        result = await action(browser_session=session)
                        self.assertEqual(json.loads(result.extracted_content.split("\n", 1)[1]), snapshot)
                        page.goto.assert_not_called()
                    else:
                        with self.assertRaises(worker.BrowserError):
                            await action(browser_session=session)
                        page.goto.assert_awaited_once_with("about:blank")

    async def test_local_browser_and_model_are_explicit_without_cloud_fallback(self):
        browser = Mock(kill=AsyncMock())
        history = Mock()
        history.is_successful.return_value = True
        history.final_result.return_value = "Observed text"
        agent = Mock(run=AsyncMock(return_value=history))
        with patch("browser_use.Browser", return_value=browser) as factory, \
             patch("browser_use.ChatOllama") as model, \
             patch("browser_use.Agent", return_value=agent) as agent_factory, \
             patch.object(worker, "validate_url", return_value="example.com"), \
             patch.object(worker, "edge_executable", return_value=Path("edge.exe")), \
             patch.object(worker, "edge_profile", return_value=Path("profile")):
            result = await worker.browse("https://example.com", "Read")
        self.assertTrue(result["ok"])
        options = factory.call_args.kwargs
        self.assertFalse(options["use_cloud"])
        self.assertTrue(options["chromium_sandbox"])
        for flag in ("enable_default_extensions", "accept_downloads", "auto_download_pdfs"):
            self.assertFalse(options[flag])
        self.assertEqual(options["permissions"], [])
        self.assertIn("--app=about:blank", options["args"])
        self.assertEqual(model.call_args.kwargs["host"], "http://127.0.0.1:11434")
        self.assertEqual(model.call_args.kwargs["model"], "qwen2.5:3b")
        self.assertFalse(agent_factory.call_args.kwargs["use_vision"])
        self.assertTrue(agent_factory.call_args.kwargs["flash_mode"])
        for flag in ("use_thinking", "use_judge", "final_response_after_failure"):
            self.assertFalse(agent_factory.call_args.kwargs[flag])
        agent.run.assert_awaited_once_with(max_steps=6)
        browser.kill.assert_awaited_once()

    async def test_real_sdk_accepts_agent_configuration_without_launching_browser(self):
        from browser_use import Agent, Browser

        history = Mock()
        history.is_successful.return_value = True
        history.final_result.return_value = "Configuration only; not runtime proof"
        previous = Path.cwd()
        with tempfile.TemporaryDirectory(prefix="ada-sdk-config-") as directory:
            try:
                os.chdir(directory)
                with patch.dict(os.environ, {"BROWSER_USE_CONFIG_DIR": str(Path(directory) / "config")}), \
                     patch.object(worker, "edge_profile", return_value=Path(directory) / "profile"), \
                     patch.object(worker, "validate_url", return_value="example.com"), \
                     patch.object(Browser, "kill", new=AsyncMock()), \
                     patch.object(Agent, "run", new=AsyncMock(return_value=history)):
                    result = await worker.browse("https://example.com", "Read")
                self.assertTrue(result["ok"])
            finally:
                os.chdir(previous)
