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

    def read_public_url(self, url: str, task: str) -> WebNavigationResponse:
        try:
            runner = self._runner if self._runner is not None else browser_navigation.run_task
            result = runner(url, task)
        except browser_navigation.BrowserError as error:
            # The legacy exception has no typed code. Match its precise public
            # messages; unknown worker/model failures must remain failures.
            code = {
                "Usa una URL HTTPS pública, sin credenciales y del dominio autorizado.": ErrorCode.PERMISSION_DENIED,
                "Prepara .venv-browser e instala requirements-browser.txt; consulta README.": ErrorCode.DEPENDENCY_MISSING,
                "La navegación agotó su tiempo y se detuvo; no se reintentó.": ErrorCode.TIMEOUT,
            }.get(str(error), ErrorCode.EXECUTION_FAILED)
            raise CapabilityError(code, self.manifest.capability, str(error),
                                  extension_id=self.manifest.extension_id) from error
        return WebNavigationResponse(result.text, result.url)

    def navigate(self, request: WebNavigationRequest) -> WebNavigationResponse:
        return self.read_public_url(request.url, request.task)
