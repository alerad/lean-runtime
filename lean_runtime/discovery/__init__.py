"""Bounded discovery of exact Lean Runtime environments."""

from .analyzer import SourceEvidence, analyze_source
from .api import Discovery, discover_and_check, plan
from .candidate import (
    PLAN_SCHEMA,
    Candidate,
    CandidateReason,
    DiscoveryPlan,
    ExcludedCandidate,
)
from .catalog import CATALOG_SCHEMA, Catalog, CatalogEntry
from .catalog_build import build_catalog, build_catalog_file
from .catalog_manifest import (
    CATALOG_SOURCE_SCHEMA,
    CatalogSourceEntry,
    CatalogSourceManifest,
)
from .defaults import DEFAULT_CATALOG_PATH, default_catalog
from .errors import CatalogError, DiscoveryError, PolicyError
from .history import DecisionHint, DiscoveryHistory
from .planner import Planner
from .policy import DiscoveryPolicy
from .probe import AcquiredCandidate, CandidateProbe, LeanRuntimeProbe, ProbeOutcome
from .result import (
    RESULT_SCHEMA,
    CandidateAttempt,
    DiscoveryResult,
    ResultDiagnostic,
)
from .schema_resources import SCHEMA_NAMES, schema_path

__all__ = [
    "CATALOG_SCHEMA",
    "CATALOG_SOURCE_SCHEMA",
    "DEFAULT_CATALOG_PATH",
    "PLAN_SCHEMA",
    "RESULT_SCHEMA",
    "Candidate",
    "CandidateAttempt",
    "AcquiredCandidate",
    "CandidateProbe",
    "CandidateReason",
    "Catalog",
    "CatalogEntry",
    "CatalogError",
    "CatalogSourceEntry",
    "CatalogSourceManifest",
    "Discovery",
    "DiscoveryError",
    "DiscoveryHistory",
    "DiscoveryPlan",
    "DiscoveryPolicy",
    "DiscoveryResult",
    "DecisionHint",
    "ExcludedCandidate",
    "LeanRuntimeProbe",
    "Planner",
    "PolicyError",
    "ProbeOutcome",
    "ResultDiagnostic",
    "SCHEMA_NAMES",
    "SourceEvidence",
    "analyze_source",
    "build_catalog",
    "build_catalog_file",
    "default_catalog",
    "discover_and_check",
    "plan",
    "schema_path",
]
