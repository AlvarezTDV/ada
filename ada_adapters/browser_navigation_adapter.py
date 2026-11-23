"""Delegate public-page reads to the existing isolated Browser Use worker."""

import browser_navigation

from ada_core import (
    CapabilityError, CapabilityKind, ErrorCode, ExecutionModel, ExtensionManifest,
    ExtensionState, HealthReport, PermissionSet, SideEffectProfile,
    WebNavigationRequest, WebNavigationResponse,
)


class BrowserUseNavigationAdapter:
    manifest = ExtensionManifest(
        "navigation.browser_use_edge", CapabilityKind.NAVIGATION, SideEffectProfile.PURE_READ,
        execution_model=ExecutionModel.SUBPROCESS_JOB,
        permissions=PermissionSet(allow_subprocess=True),
    )

    def __init__(self, *, browser_runner=None):
        self._runner = browser_runner

    @staticmethod
    def probe() -> HealthReport:
        try:
            browser_navigation.worker_python()
        except browser_navigation.BrowserError as error:
            return HealthReport(ExtensionState.UNAVAILABLE_DEPENDENCY, reason=str(error))
        return HealthReport(ExtensionState.READY)

    def prepare_navigation(self, request: WebNavigationRequest, *, observer=None):
        """Return page data, without sending the task to the browser worker."""
        try:
            browser_navigation.validate_task(request.task)
            if observer is None:
                return browser_navigation.observe_page(request.url)
            result = observer(request.url, request.task)
            # Older injected runners returned the answer, not the page observation.
            if (not isinstance(result, dict) and isinstance(getattr(result, "text", None), str)
                    and isinstance(getattr(result, "url", None), str)):
                return WebNavigationResponse(result.text, result.url)
            return result
        except browser_navigation.BrowserError as error:
            self._raise_browser_error(error)

    def _raise_browser_error(self, error):
        # Legacy injected runners may report only a public message.
        legacy_codes = {
            browser_navigation.URL_POLICY_MESSAGE: ErrorCode.PERMISSION_DENIED,
            browser_navigation.WORKER_MISSING_MESSAGE: ErrorCode.DEPENDENCY_MISSING,
            browser_navigation.WORKER_TIMEOUT_MESSAGE: ErrorCode.TIMEOUT,
        }
        code = {
            "permission_denied": ErrorCode.PERMISSION_DENIED,
            "dependency_missing": ErrorCode.DEPENDENCY_MISSING,
            "timeout": ErrorCode.TIMEOUT,
        }.get(error.code, legacy_codes.get(str(error), ErrorCode.EXECUTION_FAILED))
        raise CapabilityError(code, self.manifest.capability, str(error),
                              extension_id=self.manifest.extension_id) from error

    def read_public_url(self, url: str, task: str) -> WebNavigationResponse:
        try:
            runner = self._runner if self._runner is not None else browser_navigation.run_task
            result = runner(url, task)
        except browser_navigation.BrowserError as error:
            self._raise_browser_error(error)
        return WebNavigationResponse(result.text, result.url)

    def navigate(self, request: WebNavigationRequest) -> WebNavigationResponse:
        return self.read_public_url(request.url, request.task)
