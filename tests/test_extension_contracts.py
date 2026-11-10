"""Offline, deterministic checks for the dependency-free core contracts."""

import ast
from dataclasses import FrozenInstanceError, fields, is_dataclass
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import ada_core as core
from ada_core import domain, ports


class ExtensionContractTests(unittest.TestCase):
    def manifest(self, profile=core.SideEffectProfile.PURE_READ, **kwargs):
        return core.ExtensionManifest(
            extension_id="test.extension", capability=core.CapabilityKind.NAVIGATION,
            side_effect_profile=profile, **kwargs,
        )

    def test_ada_core_imports_only_standard_library_modules(self):
        root = Path(core.__file__).parent
        for path in sorted(root.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                for name in names:
                    with self.subTest(file=path.name, module=name):
                        self.assertIn(name, sys.stdlib_module_names | {"ada_core"})
            # Dynamic imports must not bypass the static dependency check.
            self.assertFalse(any(
                isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Name) and node.func.id == "__import__"
                    or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
                ) for node in ast.walk(tree)
            ))

    def test_timeout_error_enforces_no_blind_retry_on_mutating_operations(self):
        for profile, uncertain, retryable in (
            (core.SideEffectProfile.PURE_READ, False, True),
            (core.SideEffectProfile.IDEMPOTENT_WRITE, False, False),
            (core.SideEffectProfile.NON_IDEMPOTENT_MUTATION, True, False),
        ):
            with self.subTest(profile=profile):
                error = core.CapabilityError.from_timeout(self.manifest(profile), "Timed out")
                self.assertIsInstance(error, Exception)
                self.assertEqual(error.code, core.ErrorCode.TIMEOUT)
                self.assertEqual(error.capability, core.CapabilityKind.NAVIGATION)
                self.assertEqual(error.extension_id, "test.extension")
                self.assertEqual(error.user_message, "Timed out")
                self.assertEqual(str(error), "Timed out")
                self.assertIs(error.side_effect_uncertain, uncertain)
                self.assertIs(error.retryable, retryable)

    def test_redact_secrets_masks_groq_keys_and_env_secrets(self):
        with patch.dict(os.environ, {
            "GROQ_API_KEY": "configured-groq-secret",
            "OPENAI_API_KEY": "configured-openai-secret",
        }):
            secrets = ("configured-groq-secret", "configured-openai-secret",
                       "gsk_Test123", "Bearer abc.DEF_123-xyz", "private+literal", "private+")
            text = " | ".join(secrets) + " | ordinary text"
            clean = core.redact_secrets(text, (s for s in ("private+", "private+literal", "")))
            self.assertEqual(clean, " | ".join(["[REDACTED]"] * len(secrets)) + " | ordinary text")
            self.assertEqual(core.redact_secrets(""), "")
            self.assertEqual(core.redact_secrets("ordinary text"), "ordinary text")
            error = core.CapabilityError.from_timeout(
                self.manifest(), "configured-openai-secret Bearer abc.DEF_123-xyz"
            )
            self.assertEqual(str(error), "[REDACTED] [REDACTED]")
            self.assertNotIn("configured-openai-secret", repr(error))
            self.assertEqual(error.args, (error.user_message,))
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_Abc-extra", "OPENAI_API_KEY": ""}):
            self.assertEqual(
                core.redact_secrets("gsk_Abc-extra Bearer abc.DEF", ("gsk_", "Bearer")),
                "[REDACTED] [REDACTED]",
            )

    def test_bearer_variants_never_expose_credentials_in_errors(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "", "OPENAI_API_KEY": ""}):
            for scheme in ("Bearer", "bearer", "BEARER", "bEaReR"):
                for token in ("abc/def+ghi~jkl==", "/+~==", "simple.token_123-"):
                    with self.subTest(scheme=scheme, token=token):
                        text = f"Authorization: {scheme} {token}"
                        self.assertEqual(core.redact_secrets(text), "Authorization: [REDACTED]")
                        error = core.CapabilityError.from_timeout(self.manifest(), text)
                        self.assertEqual(error.user_message, "Authorization: [REDACTED]")
                        self.assertEqual(str(error), error.user_message)
                        self.assertEqual(error.args, (error.user_message,))

    def test_overlapping_bearer_tokens_are_fully_redacted(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "", "OPENAI_API_KEY": ""}):
            self.assertEqual(
                core.redact_secrets("Authorization: Bearer Bearer live-token.123"),
                "Authorization: [REDACTED]",
            )

    def test_overlapping_provider_tokens_are_fully_redacted(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "", "OPENAI_API_KEY": ""}):
            self.assertEqual(core.redact_secrets("gsk_ABCgsk_DEF"), "[REDACTED]")

    def test_manifest_validates_contract_version_and_immutable_permissions(self):
        hosts, paths = ["example.com"], ["relative/data"]
        permissions = core.PermissionSet(hosts, paths, False)
        manifest = self.manifest(permissions=permissions)
        hosts.append("unexpected.example")
        paths.clear()
        self.assertEqual(permissions.network_hosts, ("example.com",))
        self.assertEqual(permissions.filesystem_paths, ("relative/data",))
        self.assertFalse(permissions.allow_subprocess)
        self.assertEqual(manifest.contract_version, core.CORE_CONTRACT_VERSION)
        self.assertEqual(core.CORE_CONTRACT_VERSION, 1)
        for obj, field, value in ((permissions, "allow_subprocess", True),
                                  (manifest, "contract_version", 2)):
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, field, value)
        for version in (0, 2, True, "1"):
            with self.subTest(version=version), self.assertRaises(core.CapabilityError) as caught:
                self.manifest(contract_version=version)
            self.assertEqual(caught.exception.code, core.ErrorCode.INCOMPATIBLE_CONTRACT)
            self.assertFalse(caught.exception.retryable)

    def test_domain_models_are_immutable_and_detach_message_sequences(self):
        message = core.ChatMessage("user", "Hello")
        messages = [message]
        request = core.CompletionRequest(messages)
        messages.clear()
        self.assertEqual(request.messages, (message,))
        models = (message, request, core.CompletionResponse("Hello", "test-model"),
                  core.MemoryItem("preferences", "drink", "tea"),
                  core.SpeechOutputReport(True, "test-engine", 1),
                  core.WebNavigationRequest("https://example.com", "Read title"),
                  core.WebNavigationResponse("Title", "https://example.com"))
        self.assertEqual({mode.value for mode in domain.TurnMode}, {"casual", "task", "reasoning"})
        for model in models:
            self.assertTrue(is_dataclass(model))
            field = fields(model)[0].name
            with self.subTest(model=type(model).__name__), self.assertRaises(FrozenInstanceError):
                setattr(model, field, getattr(model, field))

    def test_capability_ports_are_runtime_checkable_and_separate_from_lifecycle(self):
        class Adapter:
            def complete(self, request):
                return core.CompletionResponse(request.messages[0].content, "test-model")

        adapter = Adapter()
        self.assertIsInstance(adapter, ports.ReasoningPort)
        self.assertNotIsInstance(adapter, ports.ExtensionLifecyclePort)
        response = adapter.complete(core.CompletionRequest((core.ChatMessage("user", "Hello"),)))
        self.assertEqual(response.text, "Hello")
        for protocol, methods in (
            (ports.ExtensionLifecyclePort, ("initialize", "shutdown")),
            (ports.MemoryPort, ("recall", "save", "delete")),
            (ports.SpeechInputPort, ("transcribe",)),
            (ports.SpeechOutputPort, ("speak",)),
            (ports.WebNavigationPort, ("navigate",)),
        ):
            implementation = type("Adapter", (), {name: lambda self, *args: None for name in methods})()
            self.assertIsInstance(implementation, protocol)
            self.assertNotIsInstance(object(), protocol)


if __name__ == "__main__":
    unittest.main()
