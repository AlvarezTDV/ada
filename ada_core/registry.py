"""Lazy extension routing, cooperative permission checks and bounded waiting.

Larger priority values win; registration order breaks ties. ``None`` admits all
IDs, while an empty allowlist admits none. Probes return ``HealthReport`` without
importing optional adapters. Dependency failures are retried only after enable().

Runtime host/subprocess brokers must call check_permission() before effects.
This is not a sandbox for untrusted Python or a process terminator: timed-out
daemon calls can still have effects, so their provider is quarantined until an
explicit enable(), which is refused while that call is still running. Neither
disable() nor swapping providers disposes adapters or revokes escaped references.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from threading import Event, RLock, Thread
import time
from typing import Any, Callable

from . import CORE_CONTRACT_VERSION
from .errors import CapabilityError, CapabilityKind, ErrorCode, redact_secrets
from .manifest import ExtensionManifest


class ExtensionState(str, Enum):
    ADMITTED = "admitted"
    DISABLED = "disabled"
    REJECTED_INCOMPATIBLE = "rejected_incompatible"
    UNAVAILABLE_DEPENDENCY = "unavailable_dependency"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True)
class HealthReport:
    state: ExtensionState
    reason: str = ""


@dataclass(frozen=True)
class ExtensionDiagnostic:
    extension_id: str
    capability: CapabilityKind
    state: ExtensionState
    active: bool
    reason: str


@dataclass
class _Extension:
    manifest: ExtensionManifest
    factory: Callable[[], Any]
    probe: Callable[[], HealthReport] | None
    priority: int
    state: ExtensionState = ExtensionState.ADMITTED
    reason: str = ""
    enabled: bool = True
    loaded: bool = False
    adapter: Any = None
    running: bool = False
    generation: int = 0


class ExtensionRegistry:
    def __init__(self, allowed_extension_ids: set[str] | None = None):
        self._allowed = (None if allowed_extension_ids is None
                         else frozenset(allowed_extension_ids))
        self._extensions: dict[str, _Extension] = {}
        self._preferred: dict[CapabilityKind, str] = {}
        self._active: dict[CapabilityKind, str] = {}
        self._lock = RLock()

    def register(self, manifest: ExtensionManifest, factory, *, probe=None, priority=100):
        """Record metadata only; never call a factory or probe during admission."""
        with self._lock:
            if manifest.extension_id in self._extensions:
                raise ValueError("Extension ID is already registered.")
            entry = _Extension(manifest, factory, probe, priority)
            reasons = []
            if (type(manifest.contract_version) is not int
                    or manifest.contract_version != CORE_CONTRACT_VERSION):
                reasons.append("Unsupported core contract version.")
            if self._allowed is not None and manifest.extension_id not in self._allowed:
                reasons.append("Extension ID is not in the allowlist.")
            if reasons:
                entry.state = ExtensionState.REJECTED_INCOMPATIBLE
                entry.enabled = False
                entry.reason = " ".join(reasons)
            self._extensions[manifest.extension_id] = entry

    def _error(self, entry, code, reason):
        return CapabilityError(code, entry.manifest.capability, reason,
                               extension_id=entry.manifest.extension_id)

    def _require_enabled(self, entry):
        if entry.state == ExtensionState.REJECTED_INCOMPATIBLE:
            raise self._error(entry, ErrorCode.INCOMPATIBLE_CONTRACT, entry.reason)
        if not entry.enabled:
            raise self._error(entry, ErrorCode.DEPENDENCY_MISSING, "Extension is disabled.")
        if entry.state == ExtensionState.UNAVAILABLE_DEPENDENCY:
            raise self._error(entry, ErrorCode.DEPENDENCY_MISSING, entry.reason)
        if entry.state == ExtensionState.FAILED:
            raise self._error(entry, ErrorCode.EXECUTION_FAILED, entry.reason)

    def enable(self, extension_id):
        """Enable or explicitly retry a failed/unavailable provider, if quiescent."""
        with self._lock:
            entry = self._extensions[extension_id]
            if entry.state == ExtensionState.REJECTED_INCOMPATIBLE:
                raise self._error(entry, ErrorCode.INCOMPATIBLE_CONTRACT, entry.reason)
            if entry.running:
                raise self._error(entry, ErrorCode.EXECUTION_FAILED,
                                  "Extension still has a running call.")
            entry.generation += 1
            entry.enabled = True
            entry.reason = ""
            entry.state = ExtensionState.READY if entry.loaded else ExtensionState.ADMITTED

    def disable(self, extension_id):
        with self._lock:
            entry = self._extensions[extension_id]
            # Rejection cannot be erased by disabling and re-enabling metadata.
            if entry.state != ExtensionState.REJECTED_INCOMPATIBLE:
                entry.enabled = False
                entry.generation += 1
                entry.state = ExtensionState.DISABLED
                entry.reason = "Extension is disabled."
                self._deactivate(entry)

    def set_preferred_provider(self, capability, extension_id):
        with self._lock:
            entry = self._extensions[extension_id]
            if entry.manifest.capability != capability:
                raise self._error(entry, ErrorCode.INCOMPATIBLE_CONTRACT,
                                  "Provider does not implement the requested capability.")
            self._require_enabled(entry)
            self._preferred[capability] = extension_id
            self._active.pop(capability, None)

    def check_permission(self, extension_id, *, host: str | None = None, subprocess=False):
        """Authorize exact declared hosts/subprocess use in runtime-owned brokers.

        Hosts are bare names, not URLs; no suffix or wildcard expansion is used.
        No network request, subprocess, or optional import is performed here.
        """
        with self._lock:
            entry = self._extensions[extension_id]
            self._require_enabled(entry)
            permissions = entry.manifest.permissions
            if ((host is not None and host not in permissions.network_hosts)
                    or (subprocess and not permissions.allow_subprocess)):
                raise self._error(entry, ErrorCode.PERMISSION_DENIED,
                                  "Requested host or subprocess permission is not declared.")

    def _deactivate(self, entry):
        capability = entry.manifest.capability
        if self._active.get(capability) == entry.manifest.extension_id:
            del self._active[capability]

    def _failed(self, entry, error):
        if entry.enabled:
            entry.state = (ExtensionState.UNAVAILABLE_DEPENDENCY
                           if error.code == ErrorCode.DEPENDENCY_MISSING else
                           ExtensionState.DEGRADED if error.code not in (
                               ErrorCode.EXECUTION_FAILED, ErrorCode.TIMEOUT
                           ) else ExtensionState.FAILED)
            entry.reason = redact_secrets(error.user_message)
            self._deactivate(entry)

    def _invoke(self, entry, operation, deadline, phase):
        with self._lock:
            self._require_enabled(entry)
            if time.monotonic() >= deadline:
                error = CapabilityError.from_timeout(entry.manifest,
                                                     "Extension call timed out.")
                self._failed(entry, error)
                raise error
            if entry.running:
                raise self._error(entry, ErrorCode.EXECUTION_FAILED,
                                  "Extension already has a running call.")
            generation = entry.generation
            entry.running = True
        done, abandoned, result = Event(), Event(), []

        def run():
            try:
                result.append((True, operation()))
            except BaseException as exc:
                # SystemExit from an optional provider must not kill the host.
                result.append((False, exc))
            finally:
                with self._lock:
                    done.set()
                    if abandoned.is_set():
                        entry.running = False

        Thread(target=run, daemon=True).start()
        completed = done.wait(max(0.0, deadline - time.monotonic()))
        with self._lock:
            abandoned.set()
            if done.is_set():
                entry.running = False
            if not completed:
                error = CapabilityError.from_timeout(entry.manifest,
                                                     "Extension call timed out.")
                # A concurrent disable does not cancel the daemon's side effects.
                # Keep its uncertainty metadata without overwriting newer state.
                if generation == entry.generation:
                    self._failed(entry, error)
                raise error from None
            if generation != entry.generation:
                raise self._error(entry, ErrorCode.EXECUTION_FAILED,
                                  "Extension was disabled or reconfigured during the call.")
            success, value = result[0]
            if not success:
                if isinstance(value, ImportError):
                    error = self._error(entry, ErrorCode.DEPENDENCY_MISSING,
                                        redact_secrets(str(value)))
                elif isinstance(value, CapabilityError):
                    error = CapabilityError(
                        value.code, entry.manifest.capability, redact_secrets(value.user_message),
                        extension_id=entry.manifest.extension_id,
                        retryable=value.retryable,
                        side_effect_uncertain=value.side_effect_uncertain,
                    )
                else:
                    error = self._error(entry, ErrorCode.EXECUTION_FAILED,
                                        redact_secrets(str(value)))
                self._failed(entry, error)
                raise error from None
            if phase == "probe":
                if not isinstance(value, HealthReport):
                    error = self._error(entry, ErrorCode.EXECUTION_FAILED,
                                        "Probe must return a HealthReport.")
                elif value.state not in (ExtensionState.READY, ExtensionState.ADMITTED,
                                         ExtensionState.DEGRADED):
                    error = self._error(
                        entry, ErrorCode.DEPENDENCY_MISSING if value.state ==
                        ExtensionState.UNAVAILABLE_DEPENDENCY else ErrorCode.EXECUTION_FAILED,
                        value.reason or "Extension probe reports unavailable.",
                    )
                else:
                    entry.state = value.state
                    entry.reason = redact_secrets(value.reason)
                    return value
                self._failed(entry, error)
                raise error from None
            if phase == "factory":
                entry.adapter = value
                entry.loaded = True
            if phase != "factory" or entry.state != ExtensionState.DEGRADED:
                entry.state = ExtensionState.READY
                entry.reason = ""
            return value

    def _resolve(self, capability, deadline):
        with self._lock:
            candidates = [e for e in self._extensions.values()
                          if e.manifest.capability == capability and e.enabled]
            preferred = self._preferred.get(capability)
            candidates.sort(key=lambda e: (e.manifest.extension_id == preferred, e.priority),
                            reverse=True)
        errors = []
        for entry in candidates:
            try:
                with self._lock:
                    self._require_enabled(entry)
                    loaded = entry.loaded
                if not loaded:
                    if entry.probe is not None:
                        self._invoke(entry, entry.probe, deadline, "probe")
                    self._invoke(entry, entry.factory, deadline, "factory")
                with self._lock:
                    self._require_enabled(entry)
                    self._active[capability] = entry.manifest.extension_id
                    return entry, entry.adapter
            except CapabilityError as error:
                errors.append(error)
                if time.monotonic() >= deadline:
                    raise
        if errors:
            # Preserve faults other than missing dependencies instead of masking them.
            fault = next((e for e in errors if e.code != ErrorCode.DEPENDENCY_MISSING), None)
            if fault:
                raise fault
        raise CapabilityError(ErrorCode.DEPENDENCY_MISSING, capability,
                              "No available provider; extensions may be missing or disabled.")

    def resolve(self, capability: CapabilityKind):
        """Resolve lazily, with a shared 30-second budget for probes and factories."""
        return self._resolve(capability, time.monotonic() + 30.0)[1]

    def execute(self, capability: CapabilityKind, fn, *, timeout_s: float = 30.0):
        """Bound resolution plus fn(adapter); never automatically retry an operation."""
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be a finite positive number.")
        deadline = time.monotonic() + timeout_s
        entry, adapter = self._resolve(capability, deadline)
        return self._invoke(entry, lambda: fn(adapter), deadline, "execute")

    def diagnostics(self) -> list[ExtensionDiagnostic]:
        """Return immutable snapshots without executing extension code."""
        with self._lock:
            return [ExtensionDiagnostic(
                e.manifest.extension_id, e.manifest.capability, e.state,
                self._active.get(e.manifest.capability) == e.manifest.extension_id,
                redact_secrets(e.reason),
            ) for e in self._extensions.values()]
