"""Declarative extension metadata; permissions grant nothing by themselves."""

from dataclasses import dataclass, field
from enum import Enum

from . import CORE_CONTRACT_VERSION
from .errors import CapabilityError, CapabilityKind, ErrorCode, SideEffectProfile


class ExecutionModel(str, Enum):
    IN_PROCESS_LAZY = "in_process_lazy"
    SUBPROCESS_JOB = "subprocess_job"


@dataclass(frozen=True)
class PermissionSet:
    """Immutable requested scope; enforcement belongs to the runtime, not core."""

    network_hosts: tuple[str, ...] = ()
    filesystem_paths: tuple[str, ...] = ()
    allow_subprocess: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "network_hosts", tuple(self.network_hosts))
        object.__setattr__(self, "filesystem_paths", tuple(self.filesystem_paths))


@dataclass(frozen=True)
class ExtensionManifest:
    extension_id: str
    capability: CapabilityKind
    side_effect_profile: SideEffectProfile
    contract_version: int = CORE_CONTRACT_VERSION
    execution_model: ExecutionModel = ExecutionModel.IN_PROCESS_LAZY
    permissions: PermissionSet = field(default_factory=PermissionSet)

    def __post_init__(self) -> None:
        if type(self.contract_version) is not int or self.contract_version != CORE_CONTRACT_VERSION:
            raise CapabilityError(
                ErrorCode.INCOMPATIBLE_CONTRACT, self.capability,
                "Unsupported core contract version.", extension_id=self.extension_id,
            )
