"""Stability studies: pull-out scheduling and tracking."""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

from .errors import ComplianceError, NotFoundError, PermissionDenied, ValidationError

ICH_LONG_TERM = (0, 3, 6, 9, 12, 18, 24, 36)
ICH_INTERMEDIATE = (0, 6, 9, 12)
ICH_ACCELERATED = (0, 3, 6)
CONDITIONS = frozenset({"25C/60%RH", "30C/65%RH", "30C/75%RH", "40C/75%RH", "5C"})
STABILITY_ROLES = frozenset({"analyst", "qa_manager"})
EARLY_WINDOW_DAYS = 7


def add_months(d: date, months: int) -> date:
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


@dataclass
class StabilityStudy:
    id: str
    product_code: str
    batch_no: str
    condition: str
    start_date: date
    timepoints: tuple[int, ...]
    pulls: dict = field(default_factory=dict)  # month -> {"on","by","late"}

    def due_date(self, month: int) -> date:
        return add_months(self.start_date, month)


class StabilityService:
    def __init__(self, tenant_id, catalog, audit, users, entitlements, clock):
        self.tenant_id, self.catalog, self.audit = tenant_id, catalog, audit
        self.users, self.entitlements, self.clock = users, entitlements, clock
        self._studies: dict[str, StabilityStudy] = {}

    def _gate(self, actor_id):
        self.entitlements.assert_module(self.tenant_id, "stability")
        if not self.users.get(actor_id).roles & STABILITY_ROLES:
            raise PermissionDenied("Role may not manage stability studies")

    def _load(self, study_id: str) -> StabilityStudy:
        if study_id not in self._studies:
            raise NotFoundError(f"Unknown stability study '{study_id}'")
        return self._studies[study_id]

    def get_study(self, study_id: str) -> StabilityStudy:
        import copy
        return copy.deepcopy(self._load(study_id))

    def list_studies(self) -> list[StabilityStudy]:
        import copy
        return [copy.deepcopy(s) for s in self._studies.values()]

    def create_study(self, actor_id, product_code, batch_no, condition, start_date: date,
                     timepoints=ICH_LONG_TERM) -> StabilityStudy:
        self._gate(actor_id)
        if not batch_no or not batch_no.strip():
            raise ValidationError("Batch number is required")
        self.catalog.product_name(product_code)
        if condition not in CONDITIONS:
            raise ValidationError(f"Unsupported storage condition '{condition}'")
        points = tuple(sorted(set(timepoints)))
        if not points or points[0] != 0 or any(p < 0 for p in points):
            raise ValidationError("Timepoints must start at month 0")
        study = StabilityStudy(f"STB-{len(self._studies) + 1:04d}", product_code, batch_no,
                               condition, start_date, points)
        self._studies[study.id] = study
        self.audit.append(actor_id, "STABILITY_STUDY_CREATED", "study", study.id,
                          {"product": product_code, "batch": batch_no, "condition": condition,
                           "timepoints": list(points)})
        return study

    def schedule(self, study_id: str, on: date) -> list[dict]:
        study = self._load(study_id)
        return [{"month": m, "due_date": study.due_date(m),
                 "state": self._state(study, m, on)} for m in study.timepoints]

    @staticmethod
    def _state(study: StabilityStudy, month: int, on: date, window: int = EARLY_WINDOW_DAYS) -> str:
        if month in study.pulls:
            return "pulled"
        due = study.due_date(month)
        if due < on:
            return "overdue"
        if due <= on + timedelta(days=window):
            return "due"
        return "upcoming"

    def pulls_needing_action(self, on: date, window_days: int = EARLY_WINDOW_DAYS) -> list[dict]:
        out = []
        for study in self._studies.values():
            for m in study.timepoints:
                state = self._state(study, m, on, window_days)
                if state in ("overdue", "due"):
                    out.append({"study_id": study.id, "month": m, "due_date": study.due_date(m),
                                "state": state})
        return sorted(out, key=lambda r: (r["due_date"], r["study_id"]))

    def record_pull(self, actor_id, study_id: str, month: int, on: date) -> dict:
        self._gate(actor_id)
        study = self._load(study_id)
        if month not in study.timepoints:
            raise ValidationError(f"Month {month} is not a scheduled timepoint")
        if month in study.pulls:
            raise ComplianceError(f"Month {month} was already pulled")
        due = study.due_date(month)
        if on < due - timedelta(days=EARLY_WINDOW_DAYS):
            raise ComplianceError(f"Too early: pull window opens {due - timedelta(days=EARLY_WINDOW_DAYS)}")
        late = on > due + timedelta(days=EARLY_WINDOW_DAYS)
        study.pulls[month] = {"on": on, "by": actor_id, "late": late}
        self.audit.append(actor_id, "STABILITY_PULL", "study", study.id,
                          {"month": month, "on": on.isoformat(), "late": late})
        return dict(study.pulls[month])
