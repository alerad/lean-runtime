"""Operation-scoped accounting. Compatibility, bytes, and references stay separate."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

Basis = Literal[
    "measured_local", "exact_publication", "exact_acquisition", "related_publication", "unknown"
]
Quantity = Literal["logical_bytes", "payload_bytes"]


@dataclass(frozen=True, slots=True)
class PackageAdoptionAction:
    action_id: str
    projects: tuple[str, ...]
    package: str
    kind: str
    package_id: str | None = None
    source_id: str | None = None
    source_path: str | None = None
    shared_path: str | None = None
    artifact_decision: str = "none"
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CostComponent:
    id: str
    action_ids: tuple[str, ...]
    category: str
    quantity: Quantity
    basis: Basis
    bytes: int | None
    complete: bool = True

    def __post_init__(self) -> None:
        if not self.action_ids or (self.bytes is not None and self.bytes < 0):
            raise ValueError("cost components require coverage and nonnegative bytes")
        if self.basis == "unknown" and self.bytes is not None:
            raise ValueError("unknown evidence cannot price a component")


@dataclass(frozen=True, slots=True)
class CostSummary:
    known_bytes: int
    complete: bool


@dataclass(frozen=True, slots=True)
class EstimateIssue:
    code: str
    subject_id: str
    projects: tuple[str, ...]
    packages: tuple[str, ...]
    detail: str
    impact: Literal["compatibility", "size", "transfer", "allocation"]


def summarize(components: tuple[CostComponent, ...], quantity: Quantity) -> CostSummary:
    # One covered action/category can have only one accounting record. Even a
    # related reference must not sit on top of a measured component.
    covered: set[tuple[str, str]] = set()
    total = 0
    complete = True
    for c in components:
        if c.quantity != quantity:
            raise ValueError("cannot sum different quantities")
        for action in c.action_ids:
            key = (action, c.category)
            if key in covered:
                raise ValueError(f"overlapping cost coverage: {key}")
            covered.add(key)
        if c.bytes is None or c.basis == "related_publication":
            complete = False
        else:
            total += c.bytes
            complete &= c.complete
    return CostSummary(total, complete)


def aggregate_issues(issues: list[EstimateIssue]) -> tuple[EstimateIssue, ...]:
    grouped: dict[tuple[str, str], EstimateIssue] = {}
    for issue in issues:
        key = (issue.code, issue.subject_id)
        old = grouped.get(key)
        grouped[key] = (
            issue
            if old is None
            else EstimateIssue(
                issue.code,
                issue.subject_id,
                tuple(sorted(set(old.projects + issue.projects))),
                tuple(sorted(set(old.packages + issue.packages))),
                issue.detail,
                issue.impact,
            )
        )
    return tuple(grouped[key] for key in sorted(grouped))


@dataclass(frozen=True, slots=True)
class AdoptionEstimates:
    components: tuple[CostComponent, ...]
    uncertainty: tuple[EstimateIssue, ...] = ()
    # Related publications are advisory and are never CostComponents.
    references: tuple[dict[str, Any], ...] = ()

    def summary(self, categories: set[str], quantity: Quantity = "logical_bytes") -> CostSummary:
        return summarize(tuple(c for c in self.components if c.category in categories), quantity)

    @property
    def local_replacement(self) -> CostSummary:
        return self.summary({"local_replacement"})

    @property
    def new_retained(self) -> CostSummary:
        return self.summary(
            {
                "source_objects",
                "package_source_trees",
                "preserved_artifacts",
                "restored_artifacts",
                "toolchains",
                "workspace_metadata",
            }
        )

    @property
    def network(self) -> CostSummary:
        return summarize(
            tuple(c for c in self.components if c.quantity == "payload_bytes"), "payload_bytes"
        )

    @property
    def net_logical_change(self) -> int | None:
        added, removed = self.new_retained, self.local_replacement
        return (
            added.known_bytes - removed.known_bytes if added.complete and removed.complete else None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "lean-runtime-adoption-estimates/1",
            "components": [asdict(c) for c in self.components],
            "local_replacement": asdict(self.local_replacement),
            "new_retained": asdict(self.new_retained),
            "network": asdict(self.network),
            "net_logical_change_bytes": self.net_logical_change,
            "known_logical_change_bytes": (
                self.new_retained.known_bytes - self.local_replacement.known_bytes
            ),
            "physical_disk_change_bytes": None,
            "additional_working_space_bytes": None,
            "allocation_note": "Unknown: copying/reflinks, extents, staging, and final references.",
            "uncertainty": [asdict(issue) for issue in self.uncertainty],
            "references": list(self.references),
        }
