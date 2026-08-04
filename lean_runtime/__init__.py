"""Content-addressed execution environments for Lean 4."""

from .audit import ArtifactInventory, AuditReport
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
    LeanCheckError,
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
from .facade import (
    DependencyInput,
    PreparedEnvironment,
    check,
    check_file,
    default_runtime,
    replay,
    setup,
)
from .frontmatter import LeanFrontmatter, load_frontmatter, parse_frontmatter
from .health import DoctorCheck, DoctorReport
from .lockfiles import EnvironmentLock, LockedPackage
from .models import (
    Diagnostic,
    ExecutionProvenance,
    ExecutionResult,
    PackageProvenance,
    ProjectProvenance,
)
from .oci import DEFAULT_CACHE_REPOSITORIES, OCIRepository, PublishInfo
from .policies import ExecutionPolicy
from .projects import ProjectContext, ProjectEnvironment, discover_project
from .references import PACKAGE_ALIASES, DiscoveredPackage, PackageReference
from .runtime import Runtime, project_toolchain
from .specs import EnvironmentSpec, GitPackage, Package
from .store import BlobGarbageCollectionReport, GarbageCollectionReport, StoreStatus
from .toolchains import ToolchainManager, normalize_toolchain

__all__ = [
    "Diagnostic",
    "BundleInfo",
    "ArtifactInventory",
    "AuditReport",
    "BlobGarbageCollectionReport",
    "DEFAULT_CACHE_REPOSITORIES",
    "DependencyInput",
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
    "LeanCheckError",
    "LeanFrontmatter",
    "LeanRuntimeError",
    "LockedPackage",
    "MaterializationError",
    "Package",
    "PackageReference",
    "PACKAGE_ALIASES",
    "PackageProvenance",
    "ProjectContext",
    "ProjectEnvironment",
    "ProjectProvenance",
    "PreparedEnvironment",
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
    "discover_project",
    "check",
    "check_file",
    "default_runtime",
    "load_frontmatter",
    "parse_frontmatter",
    "replay",
    "setup",
]
