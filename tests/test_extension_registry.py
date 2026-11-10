"""Offline microkernel checks using only synthetic, in-memory extensions."""

from dataclasses import replace
from threading import Event, Thread, current_thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from ada_core import (
    CORE_CONTRACT_VERSION, CapabilityError, CapabilityKind, CompletionResponse,
    ErrorCode, ExtensionManifest, PermissionSet, SideEffectProfile,
)
from ada_core.registry import ExtensionRegistry, ExtensionState, HealthReport


class ExtensionRegistryTests(unittest.TestCase):
    def manifest(self, extension_id="test.primary", capability=CapabilityKind.REASONING, **kw):
        return ExtensionManifest(
            extension_id, capability, SideEffectProfile.PURE_READ, **kw
        )

    def diagnostic(self, registry, extension_id="test.primary"):
        return next(d for d in registry.diagnostics() if d.extension_id == extension_id)

    def test_registry_operates_when_optional_capabilities_are_missing_or_disabled(self):
        registry = ExtensionRegistry()
        factory = Mock(return_value=object())
        registry.register(self.manifest(), factory)
        factory.assert_not_called()
        registry.register(self.manifest("speech", CapabilityKind.TTS), Mock())
        registry.disable("speech")
        self.assertIs(registry.resolve(CapabilityKind.REASONING), factory.return_value)
        for capability in (CapabilityKind.TTS, CapabilityKind.STT):
            with self.subTest(capability=capability), self.assertRaises(CapabilityError) as caught:
                registry.resolve(capability)
            self.assertEqual(caught.exception.code, ErrorCode.DEPENDENCY_MISSING)
        self.assertEqual(factory.call_count, 1)
        self.assertIs(registry.resolve(CapabilityKind.REASONING), factory.return_value)
        self.assertEqual(factory.call_count, 1)

    def test_broken_optional_dependency_import_marks_unavailable_without_crashing(self):
        for failure in (ImportError("gsk_Private123"), ModuleNotFoundError("missing"), None):
            with self.subTest(failure=failure):
                registry = ExtensionRegistry()
                factory = Mock(side_effect=failure) if failure else Mock()
                probe = None if failure else Mock(return_value=HealthReport(
                    ExtensionState.UNAVAILABLE_DEPENDENCY, "Missing optional package"
                ))
                registry.register(self.manifest(), factory, probe=probe, priority=200)
                fallback = object()
                registry.register(self.manifest("fallback"), lambda: fallback, priority=100)
                self.assertIs(registry.resolve(CapabilityKind.REASONING), fallback)
                report = self.diagnostic(registry)
                self.assertEqual(report.state, ExtensionState.UNAVAILABLE_DEPENDENCY)
                self.assertNotIn("gsk_Private123", report.reason)
                if probe:
                    factory.assert_not_called()
                registry.disable("fallback")
                with self.assertRaises(CapabilityError) as caught:
                    registry.resolve(CapabilityKind.REASONING)
                self.assertEqual(caught.exception.code, ErrorCode.DEPENDENCY_MISSING)
                self.assertEqual(report, self.diagnostic(registry))

    def test_incompatible_version_or_unlisted_id_is_rejected_with_explanation_before_factory_runs(self):
        for version, allowed in ((CORE_CONTRACT_VERSION + 1, {"test.primary"}),
                                 (CORE_CONTRACT_VERSION, set()), (True, {"test.primary"})):
            with self.subTest(version=version, allowed=allowed):
                registry = ExtensionRegistry(allowed)
                # WU-04 rejects invalid versions at construction; simulate foreign metadata.
                manifest = SimpleNamespace(**vars(self.manifest()))
                manifest.contract_version = version
                factory, probe = Mock(), Mock()
                registry.register(manifest, factory, probe=probe)
                report = self.diagnostic(registry)
                self.assertEqual(report.state, ExtensionState.REJECTED_INCOMPATIBLE)
                self.assertFalse(report.active)
                self.assertTrue(report.reason)
                with self.assertRaises(CapabilityError):
                    registry.enable("test.primary")
                with self.assertRaises(CapabilityError):
                    registry.resolve(CapabilityKind.REASONING)
                factory.assert_not_called()
                probe.assert_not_called()

    def test_swapping_provider_for_same_capability_preserves_consumer_contract(self):
        registry = ExtensionRegistry()
        for name, priority in (("first", 200), ("second", 100)):
            adapter = SimpleNamespace(complete=lambda request, n=name: CompletionResponse(n, "fake"))
            registry.register(self.manifest(name), lambda a=adapter: a, priority=priority)
        call = lambda adapter: adapter.complete(None)
        self.assertEqual(registry.execute(CapabilityKind.REASONING, call).text, "first")
        registry.set_preferred_provider(CapabilityKind.REASONING, "second")
        response = registry.execute(CapabilityKind.REASONING, call)
        self.assertIsInstance(response, CompletionResponse)
        self.assertEqual(response.text, "second")
        self.assertFalse(self.diagnostic(registry, "first").active)
        self.assertTrue(self.diagnostic(registry, "second").active)
        registry.disable("second")
        self.assertEqual(registry.execute(CapabilityKind.REASONING, call).text, "first")

    def test_hanging_or_crashing_extension_times_out_cleanly_into_capability_error(self):
        for stage in ("probe", "factory", "execute"):
            with self.subTest(stage=stage):
                registry = ExtensionRegistry()
                release, finished = Event(), Event()

                def hang(*args):
                    try:
                        release.wait()
                        return object()
                    finally:
                        finished.set()

                manifest = replace(self.manifest(), side_effect_profile=
                                   SideEffectProfile.NON_IDEMPOTENT_MUTATION)
                registry.register(manifest, hang if stage == "factory" else object,
                                  probe=hang if stage == "probe" else None)
                start = time.monotonic()
                try:
                    with self.assertRaises(CapabilityError) as caught:
                        registry.execute(CapabilityKind.REASONING,
                                         hang if stage == "execute" else lambda a: a,
                                         timeout_s=0.1)
                    self.assertLess(time.monotonic() - start, 0.3)
                    self.assertEqual(caught.exception.code, ErrorCode.TIMEOUT)
                    self.assertTrue(caught.exception.side_effect_uncertain)
                    self.assertFalse(caught.exception.retryable)
                    self.assertEqual(self.diagnostic(registry).state, ExtensionState.FAILED)
                    with self.assertRaises(CapabilityError):
                        registry.enable("test.primary")
                finally:
                    release.set()
                    self.assertTrue(finished.wait(1))
                self.assertEqual(self.diagnostic(registry).state, ExtensionState.FAILED)
        for stage in ("probe", "factory", "execute"):
            for error in (RuntimeError("Bearer private-token"), SystemExit("gsk_Private123")):
                with self.subTest(stage=stage, error=type(error)):
                    registry = ExtensionRegistry()
                    crash = Mock(side_effect=error)
                    registry.register(self.manifest(), crash if stage == "factory" else object,
                                      probe=crash if stage == "probe" else None)
                    with self.assertRaises(CapabilityError) as caught:
                        registry.execute(CapabilityKind.REASONING,
                                         crash if stage == "execute" else lambda a: a)
                    self.assertEqual(caught.exception.code, ErrorCode.EXECUTION_FAILED)
                    self.assertNotIn("private-token", str(caught.exception))
                    self.assertNotIn("gsk_Private123", self.diagnostic(registry).reason)
                    self.assertEqual(self.diagnostic(registry).state, ExtensionState.FAILED)
                    registry.register(self.manifest("healthy", CapabilityKind.MEMORY), object)
                    self.assertIsNotNone(registry.resolve(CapabilityKind.MEMORY))

    def test_disabling_active_extension_mid_session_reports_clear_error_on_next_call(self):
        registry = ExtensionRegistry()
        factory = Mock(return_value=object())
        registry.register(self.manifest(), factory)
        registry.execute(CapabilityKind.REASONING, lambda a: a)
        registry.disable("test.primary")
        call = Mock()
        with self.assertRaises(CapabilityError) as caught:
            registry.execute(CapabilityKind.REASONING, call)
        self.assertIn("disabled", str(caught.exception).lower())
        self.assertEqual(self.diagnostic(registry).state, ExtensionState.DISABLED)
        self.assertFalse(self.diagnostic(registry).active)
        call.assert_not_called()
        registry.enable("test.primary")
        self.assertIs(registry.resolve(CapabilityKind.REASONING), factory.return_value)

        # Disabling revokes future calls, but cannot cancel an in-flight mutation.
        registry = ExtensionRegistry()
        registry.register(replace(self.manifest(), side_effect_profile=
                                  SideEffectProfile.NON_IDEMPOTENT_MUTATION), object)
        started, release = Event(), Event()
        errors, workers = [], []

        def mutate(adapter):
            workers.append(current_thread())
            started.set()
            release.wait()

        def call():
            try:
                registry.execute(CapabilityKind.REASONING, mutate, timeout_s=0.1)
            except CapabilityError as error:
                errors.append(error)

        caller = Thread(target=call)
        caller.start()
        try:
            self.assertTrue(started.wait(1))
            registry.disable("test.primary")
            caller.join(1)
            self.assertFalse(caller.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0].code, ErrorCode.TIMEOUT)
            self.assertTrue(errors[0].side_effect_uncertain)
            self.assertFalse(errors[0].retryable)
            self.assertEqual(self.diagnostic(registry).state, ExtensionState.DISABLED)
        finally:
            release.set()
            caller.join(1)
            for worker in workers:
                worker.join(1)
                self.assertFalse(worker.is_alive())
        self.assertEqual(self.diagnostic(registry).state, ExtensionState.DISABLED)
        self.assertFalse(self.diagnostic(registry).active)

    def test_permission_check_denies_undeclared_host_or_subprocess(self):
        registry = ExtensionRegistry()
        registry.register(self.manifest(permissions=PermissionSet(
            network_hosts=("api.example.com",)
        )), object)
        registry.check_permission("test.primary", host="api.example.com")
        for kw in ({"host": "evil.example"}, {"host": "api.example.com.evil"},
                   {"subprocess": True}, {"host": "api.example.com", "subprocess": True}):
            with self.subTest(kw=kw), self.assertRaises(CapabilityError) as caught:
                registry.check_permission("test.primary", **kw)
            self.assertEqual(caught.exception.code, ErrorCode.PERMISSION_DENIED)
        registry.register(self.manifest("jobs", permissions=PermissionSet(
            allow_subprocess=True
        )), object)
        registry.check_permission("jobs", subprocess=True)
        registry.disable("jobs")
        with self.assertRaises(CapabilityError):
            registry.check_permission("jobs", subprocess=True)

    def test_diagnostics_reports_installed_enabled_compatible_and_available_states(self):
        registry = ExtensionRegistry()
        probe = Mock(return_value=HealthReport(ExtensionState.READY))
        registry.register(self.manifest(), object, probe=probe)
        self.assertEqual(self.diagnostic(registry).state, ExtensionState.ADMITTED)
        probe.assert_not_called()
        registry.resolve(CapabilityKind.REASONING)
        self.assertEqual(self.diagnostic(registry).state, ExtensionState.READY)
        self.assertTrue(self.diagnostic(registry).active)
        error = CapabilityError(ErrorCode.NETWORK_UNAVAILABLE, CapabilityKind.REASONING,
                                "Bearer private-token")
        with self.assertRaises(CapabilityError) as caught:
            registry.execute(CapabilityKind.REASONING, Mock(side_effect=error))
        self.assertEqual(caught.exception.code, ErrorCode.NETWORK_UNAVAILABLE)
        self.assertEqual(self.diagnostic(registry).state, ExtensionState.DEGRADED)
        self.assertNotIn("private-token", self.diagnostic(registry).reason)
        registry.execute(CapabilityKind.REASONING, lambda a: a)
        self.assertEqual(self.diagnostic(registry).state, ExtensionState.READY)
        # A provider can mutate its error after WU-04's constructor redaction.
        error.user_message = "Bearer injected-token"
        with self.assertRaises(CapabilityError) as caught:
            registry.execute(CapabilityKind.REASONING, Mock(side_effect=error))
        self.assertEqual(caught.exception.user_message, "[REDACTED]")
        self.assertEqual(caught.exception.args, ("[REDACTED]",))
        self.assertEqual(caught.exception.extension_id, "test.primary")
        self.assertNotIn("injected-token", repr(caught.exception))
        self.assertEqual(self.diagnostic(registry).reason, "[REDACTED]")
        registry.disable("test.primary")
        self.assertEqual(self.diagnostic(registry).state, ExtensionState.DISABLED)
        reports = registry.diagnostics()
        reports.clear()
        self.assertEqual(len(registry.diagnostics()), 1)


if __name__ == "__main__":
    unittest.main()
