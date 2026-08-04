"""Content-addressed execution environments for Lean 4."""

from .bundles import BundleInfo
from .environments import (
    Environment,
    EnvironmentInfo,
    ExecutionCapture,
    ExecutionJob,
    InteractiveSession,
)
from .errors import (
    EnvironmentError,
    LeanRuntimeError,
    MaterializationError,
    PolicyError,
    PrebuiltUnavailable,
    ProjectError,
    ResolutionError,
    SpecificationError,
    ToolchainError,
)
from .events import EventCallback, RuntimeEvent
from .health import DoctorCheck, DoctorReport
from .lockfiles import EnvironmentLock, LockedPackage
from .models import Diagnostic, ExecutionProvenance, ExecutionResult, PackageProvenance
from .oci import OCIRepository, PublishInfo
from .policies import ExecutionPolicy
from .references import DiscoveredPackage, PackageReference
from .runtime import Runtime, project_toolchain
from .specs import EnvironmentSpec, GitPackage, Package
from .store import GarbageCollectionReport, StoreStatus
from .toolchains import ToolchainManager, normalize_toolchain

__all__ = [
    "Diagnostic",
    "BundleInfo",
    "DiscoveredPackage",
    "DoctorCheck",
    "DoctorReport",
    "Environment",
    "EnvironmentError",
    "EnvironmentInfo",
    "EnvironmentLock",
    "EnvironmentSpec",
    "ExecutionCapture",
    "ExecutionJob",
    "InteractiveSession",
    "ExecutionPolicy",
    "ExecutionProvenance",
    "ExecutionResult",
    "EventCallback",
    "GarbageCollectionReport",
    "GitPackage",
    "LeanRuntimeError",
    "LockedPackage",
    "MaterializationError",
    "Package",
    "PackageReference",
    "PackageProvenance",
    "PolicyError",
    "PublishInfo",
    "PrebuiltUnavailable",
    "ProjectError",
    "ResolutionError",
    "Runtime",
    "RuntimeEvent",
    "OCIRepository",
    "SpecificationError",
    "StoreStatus",
    "ToolchainError",
    "ToolchainManager",
    "normalize_toolchain",
    "project_toolchain",
]
