"""Content-addressed execution environments for Lean 4."""

from .environments import Environment, EnvironmentInfo, ExecutionCapture, ExecutionJob
from .errors import (
    EnvironmentError,
    LeanRuntimeError,
    MaterializationError,
    PolicyError,
    ProjectError,
    ResolutionError,
    SpecificationError,
    ToolchainError,
)
from .lockfiles import EnvironmentLock, LockedPackage
from .models import Diagnostic, ExecutionProvenance, ExecutionResult, PackageProvenance
from .policies import ExecutionPolicy
from .runtime import Runtime, project_toolchain
from .specs import EnvironmentSpec, GitPackage, Package
from .store import GarbageCollectionReport
from .toolchains import ToolchainManager, normalize_toolchain

__all__ = [
    "Diagnostic",
    "Environment",
    "EnvironmentError",
    "EnvironmentInfo",
    "EnvironmentLock",
    "EnvironmentSpec",
    "ExecutionCapture",
    "ExecutionJob",
    "ExecutionPolicy",
    "ExecutionProvenance",
    "ExecutionResult",
    "GarbageCollectionReport",
    "GitPackage",
    "LeanRuntimeError",
    "LockedPackage",
    "MaterializationError",
    "Package",
    "PackageProvenance",
    "PolicyError",
    "ProjectError",
    "ResolutionError",
    "Runtime",
    "SpecificationError",
    "ToolchainError",
    "ToolchainManager",
    "normalize_toolchain",
    "project_toolchain",
]
