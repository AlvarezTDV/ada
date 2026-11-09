import copy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx

import browser_navigation
import memory
from test_memory_prompt import IsolatedPromptFixture, ada

# The shared voice fixture restores sys.modules after importing ada. Use the
# same SDK module as ada so discovery cannot create duplicate exception classes.
openai = ada.openai


def completion(content="OK", finish_reason="stop", *, empty_choices=False):
    choices = [] if empty_choices else [SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason=finish_reason)]
    return SimpleNamespace(choices=choices)


def status_error(error_type, status):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return error_type("Rejected gsk_secret12345", response=httpx.Response(
        status, request=request), body=None)


class GroqChatProviderTests(IsolatedPromptFixture):
    def setUp(self):
        super().setUp()
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def run_chat(self, outcomes, inputs, *, speech_enabled=False):
        client = Mock(base_url="https://api.groq.com/openai/v1")
        requests = []
        outcomes = iter(outcomes)

        def answer(**kwargs):
            requests.append(copy.deepcopy(kwargs))
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return result

        client.chat.completions.create.side_effect = answer
        with patch.object(ada, "get_client", return_value=(client, ada.DEFAULT_MODEL)), \
             patch.object(ada, "VoiceEngine") as voice, \
             patch.object(ada, "console") as console, \
             patch.object(ada, "print_header"), \
             patch.object(ada, "print_ada_response") as display, \
             patch("builtins.input", side_effect=[*inputs, KeyboardInterrupt()]):
            ada.chat_loop(speech_enabled=speech_enabled)
        return requests, console, voice.return_value, display

    def test_default_provider_is_groq_with_llama_70b_and_missing_key_raises_config_error(self):
        self.assertEqual(ada.GROQ_BASE_URL, "https://api.groq.com/openai/v1")
        self.assertEqual(ada.DEFAULT_GROQ_MODELS,
                         ("llama-3.3-70b-versatile", "openai/gpt-oss-20b"))
        self.assertEqual(ada.DEFAULT_MODEL, ada.DEFAULT_GROQ_MODELS[0])
        with patch.object(ada, "OpenAI") as factory:
            for value in (None, "", "  "):
                with self.subTest(key=value):
                    if value is not None:
                        os.environ["GROQ_API_KEY"] = value
                    with self.assertRaisesRegex(ada.ChatConfigError, "Falta GROQ_API_KEY"):
                        ada.get_client("groq")
            factory.assert_not_called()
            with patch.dict(os.environ, {"GROQ_API_KEY": " gsk_test ",
                                         "OPENAI_API_KEY": "ignored"}):
                client, model = ada.get_client()
                factory.assert_called_once_with(base_url=ada.GROQ_BASE_URL,
                                                api_key="gsk_test", timeout=25.0,
                                                max_retries=0)
                self.assertIs(client, factory.return_value)
                self.assertEqual(model, ada.DEFAULT_MODEL)
            with self.assertRaises(ada.ChatConfigError):
                ada.get_client("unknown")

    def test_cli_provider_local_selects_ollama_without_requiring_groq_key(self):
        with patch.object(ada, "OpenAI") as factory:
            client, model = ada.get_client(" LOCAL ")
            factory.assert_called_once_with(base_url="http://localhost:11434/v1",
                                            api_key="not-needed", timeout=25.0,
                                            max_retries=0)
            self.assertIs(client, factory.return_value)
            self.assertEqual(model, "qwen2.5:3b")
            with patch.dict(os.environ, {"ADA_CHAT_PROVIDER": "local",
                                         "ADA_LOCAL_MODEL": " custom-local "}):
                self.assertEqual(ada.get_client(None)[1], "custom-local")

    def test_groq_model_chain_rotates_to_gpt_oss_20b_and_beyond_on_rate_limit_429(self):
        chain = ["llama-3.3-70b-versatile", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
        with patch.dict(os.environ, {"ADA_GROQ_MODELS": " , " + ", ".join(chain) + ", "}):
            self.assertEqual(ada.get_groq_model_chain(), chain)
            for failures in (1, 2, 3):
                with self.subTest(failures=failures):
                    client = Mock(base_url=ada.GROQ_BASE_URL)
                    response = completion()
                    client.chat.completions.create.side_effect = [
                        *[status_error(openai.RateLimitError, 429) for _ in range(failures)],
                        response]
                    messages = [{"role": "user", "content": "hola"}]
                    before = copy.deepcopy(messages)
                    with patch.object(ada, "console") as console:
                        if failures == 3:
                            with self.assertRaises(openai.RateLimitError):
                                ada.create_chat_completion_with_fallback(
                                    client, chain[0], messages, temperature=0, max_tokens=300)
                        else:
                            result, used = ada.create_chat_completion_with_fallback(
                                client, chain[0], messages, temperature=0, max_tokens=300)
                            self.assertIs(result, response)
                            self.assertEqual(used, chain[failures])
                    calls = client.chat.completions.create.call_args_list
                    self.assertEqual([c.kwargs["model"] for c in calls], chain[:min(failures + 1, 3)])
                    self.assertEqual(console.print.call_count, min(failures, 2))
                    for index, notice in enumerate(console.print.call_args_list):
                        self.assertIn(f"Cuota agotada en {chain[index]}; cambiando a modelo de respaldo: {chain[index + 1]}", str(notice.args[0]))
                    self.assertNotIn("extra_body", calls[0].kwargs)
                    self.assertEqual(calls[1].kwargs["extra_body"],
                                     {"reasoning_effort": "low", "include_reasoning": False})
                    if len(calls) == 3:
                        self.assertNotIn("extra_body", calls[2].kwargs)
                    self.assertEqual(messages, before)
        for env, expected in (({}, list(ada.DEFAULT_GROQ_MODELS)),
                              ({"ADA_GROQ_MODEL": "custom"}, ["custom", *ada.DEFAULT_GROQ_MODELS]),
                              ({"ADA_GROQ_MODEL": ada.DEFAULT_MODEL}, list(ada.DEFAULT_GROQ_MODELS)),
                              ({"ADA_GROQ_MODELS": "", "ADA_GROQ_MODEL": "ignored"}, list(ada.DEFAULT_GROQ_MODELS))):
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(ada.get_groq_model_chain(), expected)
        requests, _, _, _ = self.run_chat(
            [status_error(openai.RateLimitError, 429), completion(), completion()],
            ["hola", "otra pregunta"])
        self.assertEqual([r["model"] for r in requests],
                         [ada.DEFAULT_MODEL, "openai/gpt-oss-20b", "openai/gpt-oss-20b"])

    def test_groq_instruct_requests_omit_ollama_options_while_reasoning_models_set_low_effort(self):
        messages = [{"role": "user", "content": "hola"}]
        for url, model, think, extra in (
            (ada.GROQ_BASE_URL, ada.DEFAULT_MODEL, False, None),
            (ada.GROQ_BASE_URL, "openai/gpt-oss-20b", False,
             {"reasoning_effort": "low", "include_reasoning": False}),
            (ada.GROQ_BASE_URL, "qwen/qwen3-32b", True,
             {"reasoning_effort": "medium", "include_reasoning": False}),
            (ada.DEFAULT_LOCAL_URL, ada.DEFAULT_LOCAL_MODEL, True,
             {"options": {"think": True, "num_ctx": 2048}}),
        ):
            with self.subTest(model=model):
                client = Mock(base_url=url)
                response = completion()
                client.chat.completions.create.return_value = response
                result, used = ada.create_chat_completion_with_fallback(
                    client, model, messages, temperature=0.2, max_tokens=500,
                    think=think, provider="local" if "11434" in url else "groq")
                expected = dict(model=model, messages=messages, temperature=0.2, max_tokens=500)
                if extra is not None:
                    expected["extra_body"] = extra
                client.chat.completions.create.assert_called_once_with(**expected)
                self.assertIs(result, response)
                self.assertEqual(used, model)
        for model in ("OPENAI/GPT-OSS-20B", "qwen/qwen3-32b", "deepseek-r1:8b"):
            self.assertTrue(ada._is_reasoning_model(model))
        self.assertFalse(ada._is_local_client(Mock(base_url=ada.GROQ_BASE_URL), ada.DEFAULT_LOCAL_MODEL))
        self.assertTrue(ada._is_local_client(Mock(base_url="http://127.0.0.1/v1"), "custom"))
        client = Mock(base_url=ada.DEFAULT_LOCAL_URL)
        client.chat.completions.create.side_effect = status_error(openai.RateLimitError, 429)
        with self.assertRaises(openai.RateLimitError):
            ada.create_chat_completion_with_fallback(
                client, ada.DEFAULT_MODEL, messages, temperature=0, max_tokens=50, provider="local")
        self.assertEqual(client.chat.completions.create.call_count, 1)

    def test_memory_commands_with_reasoning_model_use_safe_token_floor_and_handle_none_content(self):
        self.remember("limonada", "bebidas", "bebida")
        for text in ("anota bebida", "borra bebida"):
            for response in (completion(None), completion(empty_choices=True),
                             completion('["bebida"]' if text.startswith("borra") else
                                        '[{"key":"bebida","value":"agua"}]', "length")):
                with self.subTest(text=text, response=response):
                    client = Mock(base_url=ada.GROQ_BASE_URL)
                    client.chat.completions.create.return_value = response
                    before = Path(memory.DB_PATH).read_bytes()
                    with patch.object(ada, "console"):
                        result = ada.execute_memory_command(text, client, "openai/gpt-oss-20b")
                    self.assertIn("error", result)
                    kwargs = client.chat.completions.create.call_args.kwargs
                    self.assertGreaterEqual(kwargs["max_tokens"], 300)
                    self.assertEqual(kwargs["extra_body"],
                                     {"reasoning_effort": "low", "include_reasoning": False})
                    self.assertEqual(Path(memory.DB_PATH).read_bytes(), before)
        client = Mock(base_url=ada.GROQ_BASE_URL)
        client.chat.completions.create.side_effect = [
            status_error(openai.RateLimitError, 429),
            completion('[{"category":"bebidas","key":"bebida","value":"agua"}]')]
        with patch.object(ada, "console"):
            self.assertEqual(ada.execute_memory_command("anota bebida", client, ada.DEFAULT_MODEL),
                             "Información guardada.")
        fallback = client.chat.completions.create.call_args.kwargs
        self.assertEqual(fallback["model"], "openai/gpt-oss-20b")
        self.assertEqual(fallback["max_tokens"], 300)
        self.assertEqual(memory.get_all_memories(), [("bebidas", "bebida", "agua")])

    def test_empty_or_truncated_chat_response_retries_once_and_never_records_ellipsis_on_double_failure(self):
        for invalid in (completion(""), completion(None), completion("partial", "length"),
                        completion(empty_choices=True)):
            with self.subTest(invalid=invalid):
                requests, console, voice, display = self.run_chat(
                    [completion("first answer"), invalid, invalid, completion("next answer")],
                    ["first", "failed turn", "next"], speech_enabled=True)
                self.assertEqual(len(requests), 4)
                self.assertEqual(requests[1]["messages"], requests[2]["messages"])
                self.assertEqual(requests[3]["messages"][1:-1], [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "first answer"}])
                self.assertEqual([c.args[0] for c in display.call_args_list],
                                 ["first answer", "next answer"])
                self.assertEqual([c.args[0] for c in voice.speak.call_args_list],
                                 ["first answer", "next answer"])
                self.assertIn("vacía o truncada", str(console.print.call_args_list))
        requests, _, _, display = self.run_chat(
            [completion(None), completion("recovered")], ["hola"])
        self.assertEqual(len(requests), 2)
        display.assert_called_once_with("recovered", "casual")

    def test_provider_errors_are_translated_redact_gsk_keys_and_keep_messages_clean(self):
        request = httpx.Request("POST", ada.GROQ_BASE_URL)
        cases = [(status_error(openai.AuthenticationError, 401), "autenticación", 1),
                 (status_error(openai.PermissionDeniedError, 403), "permisos", 1),
                 (status_error(openai.RateLimitError, 429), "cuota", 2),
                 (openai.APITimeoutError(request=request), "tiempo", 1),
                 (openai.APIConnectionError(message="gsk_secret12345", request=request), "conexión", 1),
                 (status_error(openai.BadRequestError, 400), "solicitud", 1)]
        for error, expected, attempts in cases:
            with self.subTest(error=type(error).__name__):
                requests, console, voice, display = self.run_chat(
                    [completion("first answer"), *[error] * attempts, completion("next answer")],
                    ["first", "failed turn", "next"], speech_enabled=True)
                output = str(console.print.call_args_list)
                self.assertIn(expected, output.lower())
                self.assertNotIn("gsk_secret12345", output)
                self.assertNotIn("failed turn", str(requests[-1]["messages"]))
                self.assertEqual(len(requests[-1]["messages"]), 4)
                self.assertEqual(display.call_count, 2)
                self.assertEqual(voice.speak.call_count, 2)
                client = Mock(base_url=ada.GROQ_BASE_URL)
                client.chat.completions.create.side_effect = error
                self.remember("limonada", "bebidas", "bebida")
                before = Path(memory.DB_PATH).read_bytes()
                with patch.object(ada, "console"), self.assertRaises(type(error)):
                    ada.execute_memory_command("anota bebida", client, ada.DEFAULT_MODEL)
                self.assertEqual(Path(memory.DB_PATH).read_bytes(), before)
        with patch.dict(os.environ, {"GROQ_API_KEY": "nonstandard-secret"}):
            safe = ada.format_provider_error(ada.ChatConfigError(
                "Error nonstandard-secret gsk_secret12345"))
            self.assertNotIn("nonstandard-secret", safe)
            self.assertNotIn("gsk_secret12345", safe)

    def test_argparse_supports_provider_flag_defaulting_to_groq(self):
        parser = ada.build_argument_parser()
        for argv, provider, speak in (([], "groq", False),
                                      (["--provider", "local"], "local", False),
                                      (["--provider", "groq", "--speak"], "groq", True)):
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual((args.provider, args.speak), (provider, speak))
        self.assertIn("--provider", parser.format_help())

    def test_browser_worker_environment_excludes_groq_api_key(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_secret",
                                     "ADA_BROWSER_MODEL": "browser-local"}):
            env = browser_navigation.worker_environment(self.workspace)
        self.assertNotIn("GROQ_API_KEY", env)
        self.assertNotIn("gsk_secret", str(env))
        self.assertEqual(env["ADA_BROWSER_MODEL"], "browser-local")
