"""Sample lifecycle, result entry, OOS handling, review, and Certificate of Analysis."""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from .errors import (ComplianceError, NotFoundError, PermissionDenied, SignatureError,
                     TransitionError, ValidationError)
from .formula import round_half_up
from .oos import CONFIRMED, INVALIDATED, PHASE_1, PHASE_2, OosInvestigation
from .sentinel import ResultRecord

STAGES = frozenset({"raw_material", "in_process", "finished_product", "stability"})
REGISTER_ROLES = frozenset({"analyst", "qa_manager"})


@dataclass(frozen=True)
class ResultEntry:
    sample_id: str
    parameter: str
    attempt: int
    inputs: dict
    value: float
    status: str
    analyst: str
    instrument_id: str | None
    entered_at: datetime
    authorized_retest: bool = False


@dataclass
class Sample:
    id: str
    product_code: str
    spec_version: int
    batch_no: str
    stage: str
    status: str
    registered_by: str
    registered_at: str
    assigned_to: str | None = None
    results: list = field(default_factory=list)
    custody: list = field(default_factory=list)
    signatures: list = field(default_factory=list)


def _coa_digest(coa: dict) -> str:
    body = {k: v for k, v in coa.items() if k != "document_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def verify_coa(coa: dict) -> bool:
    """True if the Certificate of Analysis has not been altered since issue."""
    return coa.get("document_hash") == _coa_digest(coa)


class LabService:
    def __init__(self, tenant_id, catalog, instruments, audit, users, signer, entitlements, clock):
        self.tenant_id, self.catalog, self.instruments = tenant_id, catalog, instruments
        self.audit, self.users, self.signer = audit, users, signer
        self.entitlements, self.clock = entitlements, clock
        self._samples: dict[str, Sample] = {}
        self._oos: dict[str, OosInvestigation] = {}
        self._coa_issues: dict[str, int] = {}

    # ---- helpers -----------------------------------------------------------
    def _gate(self, actor_id, roles=None):
        self.entitlements.assert_module(self.tenant_id, "lims")
        user = self.users.get(actor_id)
        if roles is not None and not user.roles & roles:
            raise PermissionDenied("Role may not perform this action")
        return user

    def _load(self, sample_id: str) -> Sample:
        if sample_id not in self._samples:
            raise NotFoundError(f"Unknown sample '{sample_id}'")
        return self._samples[sample_id]

    def _load_oos(self, oos_id: str) -> OosInvestigation:
        if oos_id not in self._oos:
            raise NotFoundError(f"Unknown OOS investigation '{oos_id}'")
        return self._oos[oos_id]

    def _sign(self, actor_id, password, meaning, ref):
        try:
            return self.signer.sign(actor_id, password, meaning, ref)
        except SignatureError:
            self.audit.append(actor_id, "SIGNATURE_FAILED", "sample", ref, {"meaning": meaning})
            raise

    def _attempts(self, sample: Sample, parameter: str) -> list[ResultEntry]:
        return [r for r in sample.results if r.parameter == parameter]

    def _investigation_for(self, sample_id, parameter, attempt):
        for inv in self._oos.values():
            if (inv.sample_id, inv.parameter, inv.attempt) == (sample_id, parameter, attempt):
                return inv
        return None

    def _final_status(self, sample: Sample, parameter: str) -> str | None:
        """PASS, a confirmed OOS, or None if the parameter still needs work."""
        attempts = self._attempts(sample, parameter)
        if not attempts:
            return None
        last = attempts[-1]
        if last.status == "PASS":
            return "PASS"
        inv = self._investigation_for(sample.id, parameter, last.attempt)
        return "OOS" if inv is not None and inv.status == CONFIRMED else None

    # ---- samples -----------------------------------------------------------
    def register_sample(self, actor_id, product_code, batch_no, stage="finished_product",
                        spec_version=None) -> Sample:
        self._gate(actor_id, REGISTER_ROLES)
        if not batch_no or not batch_no.strip():
            raise ValidationError("Batch number is required")
        if stage not in STAGES:
            raise ValidationError(f"Unknown stage '{stage}'")
        spec = self.catalog.get_spec(product_code, spec_version)
        now = self.clock.now()
        sample = Sample(f"S-{now.year}-{len(self._samples) + 1:05d}", product_code, spec.version,
                        batch_no, stage, "Registered", actor_id, now.isoformat())
        sample.custody.append({"event": "REGISTERED", "by": actor_id, "at": now.isoformat()})
        self._samples[sample.id] = sample
        self.audit.append(actor_id, "REGISTER", "sample", sample.id,
                          {"product": product_code, "batch": batch_no, "spec_version": spec.version})
        return copy.deepcopy(sample)

    def assign(self, actor_id, sample_id, analyst_id) -> Sample:
        self._gate(actor_id, REGISTER_ROLES)
        sample = self._load(sample_id)
        if sample.status not in ("Registered", "InTesting") or sample.results:
            raise TransitionError("Sample can no longer be (re)assigned")
        if "analyst" not in self.users.get(analyst_id).roles:
            raise PermissionDenied("Assignee must be an analyst")
        sample.assigned_to, sample.status = analyst_id, "InTesting"
        sample.custody.append({"event": "ASSIGNED", "to": analyst_id, "by": actor_id,
                               "at": self.clock.now().isoformat()})
        self.audit.append(actor_id, "ASSIGN", "sample", sample.id, {"to": analyst_id})
        return copy.deepcopy(sample)

    def get_sample(self, sample_id: str) -> Sample:
        return copy.deepcopy(self._load(sample_id))

    def list_samples(self, status: str | None = None) -> list[Sample]:
        return [copy.deepcopy(s) for s in self._samples.values()
                if status is None or s.status == status]

    # ---- results -----------------------------------------------------------
    def enter_result(self, actor_id, sample_id, parameter, values, instrument_id=None) -> ResultEntry:
        self._gate(actor_id, frozenset({"analyst"}))
        sample = self._load(sample_id)
        if sample.status != "InTesting":
            raise TransitionError(f"Sample is '{sample.status}', results cannot be entered")
        if sample.assigned_to != actor_id:
            raise PermissionDenied("Only the assigned analyst may enter results")
        param = self.catalog.get_spec(sample.product_code, sample.spec_version).parameter(parameter)

        attempts = self._attempts(sample, parameter)
        consumed_inv = None
        if attempts:
            last = attempts[-1]
            if last.status == "PASS":
                raise ComplianceError("A passing result already exists; retesting is not permitted")
            inv = self._investigation_for(sample_id, parameter, last.attempt)
            if inv is None or not (inv.status == INVALIDATED and inv.retest_authorized):
                raise ComplianceError("Retest not authorised: complete the OOS investigation first")
            consumed_inv = inv

        if param.instrument_required and not instrument_id:
            raise ValidationError(f"'{parameter}' requires an instrument")
        if instrument_id:
            self.instruments.assert_usable(instrument_id, self.clock.now().date())

        value = param.compute(values)
        status = param.spec.evaluate(value)
        entry = ResultEntry(sample_id, parameter, len(attempts) + 1, dict(values), value, status,
                            actor_id, instrument_id, self.clock.now(),
                            authorized_retest=bool(attempts))
        sample.results.append(entry)
        if consumed_inv is not None:
            consumed_inv.retest_authorized = False
            consumed_inv.retest_attempt = entry.attempt
        self.audit.append(actor_id, "RESULT_ENTERED", "sample", sample_id,
                          {"parameter": parameter, "attempt": entry.attempt, "value": value,
                           "status": status, "instrument": instrument_id})
        if status == "OOS":
            inv = OosInvestigation(f"OOS-{len(self._oos) + 1:04d}", sample_id, parameter,
                                   entry.attempt, value, self.clock.now().isoformat())
            self._oos[inv.id] = inv
            self.audit.append(actor_id, "OOS_OPENED", "oos", inv.id,
                              {"sample": sample_id, "parameter": parameter, "value": value})
        return entry

    # ---- OOS ----------------------------------------------------------------
    def investigations(self, sample_id: str | None = None) -> list[OosInvestigation]:
        return [copy.deepcopy(i) for i in self._oos.values()
                if sample_id is None or i.sample_id == sample_id]

    def get_investigation(self, oos_id: str) -> OosInvestigation:
        return copy.deepcopy(self._load_oos(oos_id))

    def _check_independent(self, inv: OosInvestigation, actor_id: str):
        sample = self._load(inv.sample_id)
        analysts = {r.analyst for r in self._attempts(sample, inv.parameter)}
        if actor_id in analysts:
            raise PermissionDenied("The investigator must be independent of the original analyst")

    def conclude_phase1(self, actor_id, oos_id, assignable_cause: bool, notes: str, password) -> OosInvestigation:
        self._gate(actor_id, frozenset({"qa_manager"}))
        inv = self._load_oos(oos_id)
        if inv.status != PHASE_1:
            raise TransitionError(f"Investigation is in '{inv.status}'")
        self._check_independent(inv, actor_id)
        if not notes or not notes.strip():
            raise ValidationError("Investigation notes are required")
        sig = self._sign(actor_id, password, "OOS Phase I concluded", inv.id)
        if assignable_cause:
            inv.status, inv.retest_authorized = INVALIDATED, True
        else:
            inv.status = PHASE_2
        inv.history.append({"phase": 1, "assignable_cause": assignable_cause,
                            "by": sig.signer_name, "at": sig.timestamp})
        self.audit.append(actor_id, "OOS_PHASE1", "oos", inv.id,
                          {"assignable_cause": assignable_cause, "outcome": inv.status}, notes)
        return copy.deepcopy(inv)

    def conclude_phase2(self, actor_id, oos_id, confirmed: bool, notes: str, password) -> OosInvestigation:
        self._gate(actor_id, frozenset({"qa_manager"}))
        inv = self._load_oos(oos_id)
        if inv.status != PHASE_2:
            raise TransitionError(f"Investigation is in '{inv.status}'")
        self._check_independent(inv, actor_id)
        if not notes or not notes.strip():
            raise ValidationError("Investigation notes are required")
        sig = self._sign(actor_id, password, "OOS Phase II concluded", inv.id)
        if confirmed:
            inv.status = CONFIRMED
        else:
            inv.status, inv.retest_authorized = INVALIDATED, True
        inv.history.append({"phase": 2, "confirmed": confirmed, "by": sig.signer_name,
                            "at": sig.timestamp})
        self.audit.append(actor_id, "OOS_PHASE2", "oos", inv.id,
                          {"confirmed": confirmed, "outcome": inv.status}, notes)
        return copy.deepcopy(inv)

    # ---- review ---------------------------------------------------------------
    def submit_for_review(self, actor_id, sample_id) -> Sample:
        self._gate(actor_id, frozenset({"analyst"}))
        sample = self._load(sample_id)
        if sample.status != "InTesting":
            raise TransitionError(f"Sample is '{sample.status}'")
        if sample.assigned_to != actor_id:
            raise PermissionDenied("Only the assigned analyst may submit this sample")
        spec = self.catalog.get_spec(sample.product_code, sample.spec_version)
        pending = [p.name for p in spec.parameters if self._final_status(sample, p.name) is None]
        if pending:
            raise ComplianceError(f"Parameters without a final result: {pending}")
        sample.status = "UnderReview"
        self.audit.append(actor_id, "SUBMIT_REVIEW", "sample", sample_id, {})
        return copy.deepcopy(sample)

    def _review_gate(self, actor_id, sample_id):
        self._gate(actor_id, frozenset({"reviewer", "qa_manager"}))
        sample = self._load(sample_id)
        if sample.status != "UnderReview":
            raise TransitionError(f"Sample is '{sample.status}'")
        if actor_id in {r.analyst for r in sample.results}:
            raise PermissionDenied("Segregation of duties: the analyst cannot review their own work")
        return sample

    def approve(self, actor_id, sample_id, password) -> Sample:
        sample = self._review_gate(actor_id, sample_id)
        spec = self.catalog.get_spec(sample.product_code, sample.spec_version)
        if any(self._final_status(sample, p.name) != "PASS" for p in spec.parameters):
            raise ComplianceError("A sample with a confirmed OOS result cannot be approved")
        sig = self._sign(actor_id, password, "Approved", sample_id)
        sample.signatures.append(sig)
        sample.status = "Approved"
        self.audit.append(actor_id, "APPROVE", "sample", sample_id, {})
        return copy.deepcopy(sample)

    def reject(self, actor_id, sample_id, password, reason: str) -> Sample:
        sample = self._review_gate(actor_id, sample_id)
        if not reason or not reason.strip():
            raise ValidationError("A reason is required to reject a sample")
        sig = self._sign(actor_id, password, "Rejected", sample_id)
        sample.signatures.append(sig)
        sample.status = "Rejected"
        self.audit.append(actor_id, "REJECT", "sample", sample_id, {}, reason)
        return copy.deepcopy(sample)

    # ---- Certificate of Analysis -----------------------------------------------
    def issue_coa(self, actor_id, sample_id) -> dict:
        self._gate(actor_id, frozenset({"reviewer", "qa_manager"}))
        sample = self._load(sample_id)
        if sample.status != "Approved":
            raise ComplianceError("A COA can only be issued for an approved sample")
        spec = self.catalog.get_spec(sample.product_code, sample.spec_version)
        rows = []
        for p in spec.parameters:
            last = self._attempts(sample, p.name)[-1]
            reported = round_half_up(last.value, p.spec.decimals) \
                if p.spec.decimals is not None else last.value
            rows.append({"parameter": p.name, "value": reported, "unit": p.unit,
                         "limit_low": p.spec.low, "limit_high": p.spec.high,
                         "status": last.status, "method": p.method, "analyst": last.analyst})
        issue_no = self._coa_issues.get(sample_id, 0) + 1
        self._coa_issues[sample_id] = issue_no
        coa = {"coa_number": f"COA-{sample_id}-{issue_no}", "sample_id": sample_id,
               "product_code": sample.product_code,
               "product_name": self.catalog.product_name(sample.product_code),
               "batch_no": sample.batch_no, "spec_version": sample.spec_version,
               "stage": sample.stage, "results": rows, "disposition": "Complies",
               "approved_by": [{"name": s.signer_name, "meaning": s.meaning, "at": s.timestamp}
                               for s in sample.signatures],
               "issued_by": actor_id, "issued_at": self.clock.now().isoformat()}
        coa["document_hash"] = _coa_digest(coa)
        self.audit.append(actor_id, "COA_ISSUED", "sample", sample_id,
                          {"coa_number": coa["coa_number"], "hash": coa["document_hash"]})
        return coa

    # ---- export for the Sentinel -------------------------------------------------
    def result_records(self) -> list[ResultRecord]:
        out = []
        for sample in self._samples.values():
            spec = self.catalog.get_spec(sample.product_code, sample.spec_version)
            for r in sample.results:
                p = spec.parameter(r.parameter)
                out.append(ResultRecord(r.sample_id, r.parameter, r.attempt, r.value, r.status,
                                        r.analyst, r.instrument_id, r.entered_at, p.spec.low,
                                        p.spec.high, p.spec.decimals, r.authorized_retest))
        return out
