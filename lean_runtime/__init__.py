"""Content-addressed execution environments for Lean 4."""

from .bundles import BundleInfo
from .decisions import Decision
from .diffing import ContextDiff, DiffEntry
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
    check_matrix,
    check_matrix_async,
    default_runtime,
    replay,
    setup,
)
from .frontmatter import LeanFrontmatter, load_frontmatter, parse_frontmatter
from .health import DoctorCheck, DoctorReport
from .lockfiles import EnvironmentLock, LockedPackage
from .matrix import MatrixContext, MatrixEntry, MatrixResult
from .models import (
    Diagnostic,
    ExecutionProvenance,
    ExecutionResult,
    PackageProvenance,
    PhaseTiming,
    ProjectProvenance,
)
from .oci import DEFAULT_CACHE_REPOSITORIES, OCIRepository, PublishInfo
from .policies import ExecutionPolicy
from .profiling import ProfileReport
from .projects import ProjectContext, ProjectEnvironment, discover_project
from .references import PACKAGE_ALIASES, DiscoveredPackage, PackageReference
from .runtime import Runtime, project_toolchain
from .specs import EnvironmentSpec, GitPackage, Package
from .store import BlobGarbageCollectionReport, GarbageCollectionReport, StoreStatus
from .toolchains import ToolchainManager, normalize_toolchain
from .verification import VerificationCheck, VerificationReport

__all__ = [
    "Diagnostic",
    "Decision",
    "ContextDiff",
    "DiffEntry",
    "BundleInfo",
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
    "MatrixContext",
    "MatrixEntry",
    "MatrixResult",
    "Package",
    "PackageReference",
    "PACKAGE_ALIASES",
    "PackageProvenance",
    "PhaseTiming",
    "ProjectContext",
    "ProjectEnvironment",
    "ProjectProvenance",
    "ProfileReport",
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
    "VerificationCheck",
    "VerificationReport",
    "normalize_toolchain",
    "project_toolchain",
    "discover_project",
    "check",
    "check_file",
    "check_matrix",
    "check_matrix_async",
    "default_runtime",
    "load_frontmatter",
    "parse_frontmatter",
    "replay",
    "setup",
]
