"""Content-addressed execution environments for Lean 4."""

from importlib.metadata import version as _distribution_version

from .bundles import PortableCopyInfo
from .comparison import ComparisonEntry, EnvironmentComparison
from .decisions import Decision
from .environments import (
    Environment,
    EnvironmentInfo,
    ExecutionCapture,
    ExecutionJob,
    InteractiveSession,
)
from .errors import (
    DownloadUnavailable,
    EnvironmentError,
    LeanCheckError,
    LeanRuntimeError,
    MaterializationError,
    PolicyError,
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
from .oci import DEFAULT_ENVIRONMENT_LIBRARIES, PublicationInfo
from .policies import ExecutionPolicy
from .profiling import ProfileReport
from .programs import ProgramDescription, ProgramInfo, ReadyProgram
from .projects import ProjectContext, ProjectEnvironment, discover_project
from .references import PACKAGE_ALIASES, DiscoveredPackage, PackageReference
from .runtime import Runtime, project_toolchain
from .schema_resources import SCHEMA_NAMES, schema_path
from .specs import EnvironmentSpec, GitPackage, Package
from .store import CleanupReport, DownloadCleanupReport, StoreStatus
from .toolchains import ToolchainManager, normalize_toolchain
from .verification import VerificationCheck, VerificationReport

__version__ = _distribution_version("lean-runtime")

__all__ = [
    "Diagnostic",
    "Decision",
    "EnvironmentComparison",
    "ComparisonEntry",
    "PortableCopyInfo",
    "DownloadCleanupReport",
    "DEFAULT_ENVIRONMENT_LIBRARIES",
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
    "CleanupReport",
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
    "ProgramDescription",
    "ProgramInfo",
    "ReadyProgram",
    "PreparedEnvironment",
    "PolicyError",
    "PublicationInfo",
    "DownloadUnavailable",
    "ProjectError",
    "ResolutionError",
    "Runtime",
    "SCHEMA_NAMES",
    "RuntimeEvent",
    "SpecificationError",
    "StoreStatus",
    "ToolchainError",
    "ToolchainManager",
    "VerificationCheck",
    "VerificationReport",
    "__version__",
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
    "schema_path",
]
