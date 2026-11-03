"""Instrument register: only calibrated, in-service instruments may generate results."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from .errors import ComplianceError, NotFoundError, PermissionDenied, ValidationError

ADMIN_ROLES = frozenset({"admin", "qa_manager"})


@dataclass
class Instrument:
    instrument_id: str
    name: str
    calibration_due: date
    in_service: bool = True
    out_of_service_reason: str | None = None


class InstrumentRegistry:
    def __init__(self, tenant_id, audit, users, clock):
        self.tenant_id, self.audit, self.users, self.clock = tenant_id, audit, users, clock
        self._items: dict[str, Instrument] = {}

    def _gate(self, actor_id):
        if not self.users.get(actor_id).roles & ADMIN_ROLES:
            raise PermissionDenied("Role may not manage instruments")

    def _load(self, instrument_id: str) -> Instrument:
        if instrument_id not in self._items:
            raise NotFoundError(f"Unknown instrument '{instrument_id}'")
        return self._items[instrument_id]

    def get_instrument(self, instrument_id: str) -> Instrument:
        inst = self._load(instrument_id)
        return Instrument(inst.instrument_id, inst.name, inst.calibration_due, inst.in_service, inst.out_of_service_reason)

    def list_instruments(self) -> list[Instrument]:
        return [Instrument(i.instrument_id, i.name, i.calibration_due, i.in_service, i.out_of_service_reason)
                for i in self._items.values()]

    def register(self, actor_id, instrument_id, name, calibration_due: date) -> None:
        self._gate(actor_id)
        if instrument_id in self._items:
            raise ValidationError(f"Instrument '{instrument_id}' already registered")
        self._items[instrument_id] = Instrument(instrument_id, name, calibration_due)
        self.audit.append(actor_id, "INSTRUMENT_REGISTERED", "instrument", instrument_id,
                          {"calibration_due": calibration_due.isoformat()})

    def record_calibration(self, actor_id, instrument_id, calibrated_on: date, next_due: date) -> None:
        self._gate(actor_id)
        inst = self._load(instrument_id)
        if next_due <= calibrated_on:
            raise ValidationError("Next due date must be after the calibration date")
        inst.calibration_due = next_due
        inst.in_service, inst.out_of_service_reason = True, None
        self.audit.append(actor_id, "INSTRUMENT_CALIBRATED", "instrument", instrument_id,
                          {"calibrated_on": calibrated_on.isoformat(), "next_due": next_due.isoformat()})

    def set_out_of_service(self, actor_id, instrument_id, reason: str) -> None:
        self._gate(actor_id)
        if not reason.strip():
            raise ValidationError("A reason is required")
        inst = self._load(instrument_id)
        inst.in_service, inst.out_of_service_reason = False, reason
        self.audit.append(actor_id, "INSTRUMENT_OUT_OF_SERVICE", "instrument", instrument_id, {}, reason)

    def assert_usable(self, instrument_id: str, on: date) -> None:
        inst = self._load(instrument_id)
        if not inst.in_service:
            raise ComplianceError(f"Instrument '{instrument_id}' is out of service")
        if on > inst.calibration_due:
            raise ComplianceError(
                f"Instrument '{instrument_id}' calibration expired on {inst.calibration_due.isoformat()}")
