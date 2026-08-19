"""Compatibility wrapper for the compiler-backed discovery history."""

from __future__ import annotations

from .analyzer import SourceEvidence
from .history import DecisionHint, DiscoveryHistory
from .policy import DiscoveryPolicy


class DecisionMemo(DiscoveryHistory):
    """Deprecated name retained for API compatibility."""

    def lookup(  # type: ignore[override]
        self,
        source: str,
        evidence: SourceEvidence,
        catalog_digest: str | None = None,
        policy: DiscoveryPolicy | None = None,
    ) -> DecisionHint | None:
        del catalog_digest, policy
        return super().lookup(source, evidence)

    def remember(
        self,
        source: str,
        evidence: SourceEvidence,
        catalog_digest: str,
        policy: DiscoveryPolicy,
        lock_id: str,
    ) -> None:
        del catalog_digest, policy
        self.remember_success(source, evidence, lock_id)


__all__ = ["DecisionHint", "DecisionMemo"]
