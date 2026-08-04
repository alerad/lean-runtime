"""Stable lifecycle decision vocabulary used by explanations and JSON."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Decision:
    code: str
    subject: str
    outcome: str
    reason: str | None = None
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "subject": self.subject,
            "outcome": self.outcome,
            "reason": self.reason,
            "details": self.details or {},
        }
