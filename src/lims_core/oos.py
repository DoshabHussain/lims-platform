"""Out-of-specification (OOS) investigation record."""
from __future__ import annotations

from dataclasses import dataclass, field

PHASE_1 = "Phase I"
PHASE_2 = "Phase II"
INVALIDATED = "Invalidated"
CONFIRMED = "Confirmed"


@dataclass
class OosInvestigation:
    id: str
    sample_id: str
    parameter: str
    attempt: int
    value: float
    opened_at: str
    status: str = PHASE_1
    retest_authorized: bool = False
    retest_attempt: int | None = None
    history: list = field(default_factory=list)
