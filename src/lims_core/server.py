"""FastAPI Backend Server for LabSentinel LIMS.

Provides a full REST API for the LIMS platform and serves the interactive UI.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .clock import FixedClock
from .config import ParameterSpec
from .container import LimsPlatform
from .errors import LimsError
from .formula import Specification
from .lab import verify_coa
from .sentinel import SentinelConfig


def create_demo_workspace():
    clock = FixedClock(datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc))  # a Monday 9:00 UTC
    platform = LimsPlatform(clock)
    lab = platform.onboard("demo-lab", "enterprise", datetime(2027, 1, 1, tzinfo=timezone.utc))

    # Add default team
    users = [
        ("ann", "Ann Analyst", {"analyst"}),
        ("ali", "Ali Analyst", {"analyst"}),
        ("rev", "Rita Reviewer", {"reviewer"}),
        ("qam", "Qasim QA", {"qa_manager"}),
        ("adm", "Adnan Admin", {"admin"}),
        ("dual", "Dana Dual", {"analyst", "reviewer"}),
    ]
    for uid, name, roles in users:
        lab.users.add_user(uid, name, f"pw-{uid}", roles)

    # Catalog & Specs
    lab.catalog.define_product("adm", "PARA500", "Paracetamol 500 mg Tablets")
    lab.catalog.publish_spec("adm", "PARA500", [
        ParameterSpec("Assay", "%", Specification(95.0, 105.0, 1),
                      method="HPLC-ASSAY-01",
                      formula="(sample_area / std_area) * (std_weight / sample_weight) * purity",
                      instrument_required=True),
        ParameterSpec("pH", "", Specification(5.5, 6.5, 1)),
        ParameterSpec("Water", "%", Specification(high=0.5, decimals=2)),
    ])

    lab.catalog.define_product("adm", "IBU400", "Ibuprofen 400 mg Coated Tablets")
    lab.catalog.publish_spec("adm", "IBU400", [
        ParameterSpec("Assay", "%", Specification(97.0, 103.0, 1),
                      method="HPLC-ASSAY-02",
                      formula="(sample_area / std_area) * (std_weight / sample_weight) * purity",
                      instrument_required=True),
        ParameterSpec("Dissolution", "%", Specification(low=80.0, decimals=1), method="DISSO-01"),
    ])

    # Instruments
    lab.instruments.register("adm", "HPLC-01", "HPLC System 1 (Waters Alliance)", date(2026, 12, 31))
    lab.instruments.register("adm", "HPLC-02", "HPLC System 2 (Agilent 1260)", date(2026, 8, 15))
    lab.instruments.register("adm", "PH-MET-01", "pH Meter SevenDirect", date(2026, 10, 1))

    # Seed Sample 1: Released sample with verified COA
    ok_vals = {"sample_area": 1000, "std_area": 1000, "std_weight": 50, "sample_weight": 50, "purity": 100.0}
    s1 = lab.lab.register_sample("ann", "PARA500", "B-2601-A")
    lab.lab.assign("ann", s1.id, "ann")
    lab.lab.enter_result("ann", s1.id, "Assay", ok_vals, "HPLC-01")
    lab.lab.enter_result("ann", s1.id, "pH", {"result": 6.0})
    lab.lab.enter_result("ann", s1.id, "Water", {"result": 0.22})
    lab.lab.submit_for_review("ann", s1.id)
    lab.lab.approve("rev", s1.id, "pw-rev")
    lab.lab.issue_coa("rev", s1.id)

    # Seed Sample 2: Sample in testing with an open OOS
    s2 = lab.lab.register_sample("ann", "PARA500", "B-2601-B")
    lab.lab.assign("ann", s2.id, "ann")
    lab.lab.enter_result("ann", s2.id, "Assay", {**ok_vals, "sample_area": 1115}, "HPLC-01")  # Assay = 111.5% OOS

    # Seed Sample 3: Sample in testing
    s3 = lab.lab.register_sample("ali", "IBU400", "B-IBU-901")
    lab.lab.assign("ali", s3.id, "ali")

    # Seed Stability Study
    lab.stability.create_study("qam", "PARA500", "B-2601-A", "25C/60%RH", date(2026, 1, 5))

    return platform, lab, clock


platform, lab, clock = create_demo_workspace()

app = FastAPI(
    title="LabSentinel LIMS API",
    description="GxP-compliant Laboratory Information Management System with Data Integrity Sentinel",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------- Request/Response Models -----------------

class AdvanceClockRequest(BaseModel):
    days: int = 0
    hours: int = 0
    minutes: int = 0
    seconds: int = 0

class RegisterSampleRequest(BaseModel):
    actor_id: str
    product_code: str
    batch_no: str
    stage: str = "finished_product"
    spec_version: Optional[int] = None

class AssignSampleRequest(BaseModel):
    actor_id: str
    analyst_id: str

class EnterResultRequest(BaseModel):
    actor_id: str
    parameter: str
    values: Dict[str, Any]
    instrument_id: Optional[str] = None

class SubmitReviewRequest(BaseModel):
    actor_id: str

class ApproveSampleRequest(BaseModel):
    actor_id: str
    password: str

class RejectSampleRequest(BaseModel):
    actor_id: str
    password: str
    reason: str

class ConcludePhase1Request(BaseModel):
    actor_id: str
    assignable_cause: bool
    notes: str
    password: str

class ConcludePhase2Request(BaseModel):
    actor_id: str
    confirmed: bool
    notes: str
    password: str

class RegisterInstrumentRequest(BaseModel):
    actor_id: str
    instrument_id: str
    name: str
    calibration_due: date

class CalibrateInstrumentRequest(BaseModel):
    actor_id: str
    calibrated_on: date
    next_due: date

class OutOfServiceRequest(BaseModel):
    actor_id: str
    reason: str

class CreateStabilityStudyRequest(BaseModel):
    actor_id: str
    product_code: str
    batch_no: str
    condition: str
    start_date: date

class RecordPullRequest(BaseModel):
    actor_id: str
    month: int
    on_date: date

class AmvPrecisionRequest(BaseModel):
    values: List[float]
    max_rsd: float

class AmvAccuracyRequest(BaseModel):
    pairs: List[List[float]]  # [[added, found], ...]
    low: float = 98.0
    high: float = 102.0

class AmvLinearityRequest(BaseModel):
    x: List[float]
    y: List[float]
    min_r2: float = 0.999

class AmvLodLoqRequest(BaseModel):
    residual_sd: float
    slope: float


# ----------------- API Endpoints -----------------

@app.get("/api/system/status")
def get_system_status():
    users = lab.users.list_users()
    samples = lab.lab.list_samples()
    oos = lab.lab.investigations()
    insts = lab.instruments.list_instruments()
    studies = lab.stability.list_studies()
    plan = platform.entitlements.plan_for("demo-lab")

    return {
        "tenant_id": lab.tenant_id,
        "plan": plan.name,
        "modules": list(plan.modules),
        "clock": clock.now().isoformat(),
        "users": [{"user_id": u.user_id, "full_name": u.full_name, "roles": list(u.roles)} for u in users],
        "stats": {
            "samples_total": len(samples),
            "samples_registered": sum(1 for s in samples if s.status == "Registered"),
            "samples_in_testing": sum(1 for s in samples if s.status == "InTesting"),
            "samples_under_review": sum(1 for s in samples if s.status == "UnderReview"),
            "samples_approved": sum(1 for s in samples if s.status == "Approved"),
            "samples_rejected": sum(1 for s in samples if s.status == "Rejected"),
            "open_oos": sum(1 for i in oos if i.status in ("Phase I", "Phase II")),
            "instruments_total": len(insts),
            "instruments_in_service": sum(1 for i in insts if i.in_service),
            "active_stability_studies": len(studies),
            "audit_entries_count": len(lab.audit.entries()),
        }
    }

@app.post("/api/clock/advance")
def advance_clock(req: AdvanceClockRequest):
    clock.advance(days=req.days, hours=req.hours, minutes=req.minutes, seconds=req.seconds)
    return {"now": clock.now().isoformat()}

@app.get("/api/products")
def list_products():
    prod_map = lab.catalog.list_products()
    result = []
    for code, name in prod_map.items():
        specs = lab.catalog.list_specs(code)
        latest_spec = specs[-1] if specs else None
        params_info = []
        if latest_spec:
            for p in latest_spec.parameters:
                params_info.append({
                    "name": p.name,
                    "unit": p.unit,
                    "method": p.method,
                    "formula": p.formula,
                    "inputs": list(p.inputs),
                    "instrument_required": p.instrument_required,
                    "low": p.spec.low,
                    "high": p.spec.high,
                    "decimals": p.spec.decimals,
                })
        result.append({
            "code": code,
            "name": name,
            "version": latest_spec.version if latest_spec else 1,
            "parameters": params_info,
        })
    return result

@app.get("/api/samples")
def list_samples(status: Optional[str] = None):
    samples = lab.lab.list_samples(status)
    out = []
    for s in samples:
        spec = lab.catalog.get_spec(s.product_code, s.spec_version)
        results_formatted = []
        for r in s.results:
            results_formatted.append({
                "parameter": r.parameter,
                "attempt": r.attempt,
                "value": r.value,
                "status": r.status,
                "analyst": r.analyst,
                "instrument_id": r.instrument_id,
                "entered_at": r.entered_at.isoformat(),
                "authorized_retest": r.authorized_retest,
                "inputs": r.inputs,
            })
        out.append({
            "id": s.id,
            "product_code": s.product_code,
            "product_name": lab.catalog.product_name(s.product_code),
            "spec_version": s.spec_version,
            "batch_no": s.batch_no,
            "stage": s.stage,
            "status": s.status,
            "registered_by": s.registered_by,
            "registered_at": s.registered_at,
            "assigned_to": s.assigned_to,
            "results": results_formatted,
            "custody": s.custody,
            "signatures": [{"name": sig.signer_name, "meaning": sig.meaning, "at": sig.timestamp}
                           for sig in s.signatures],
            "parameters": [p.name for p in spec.parameters],
        })
    return out

@app.get("/api/samples/{sample_id}")
def get_sample(sample_id: str):
    try:
        s = lab.lab.get_sample(sample_id)
        spec = lab.catalog.get_spec(s.product_code, s.spec_version)
        return {
            "id": s.id,
            "product_code": s.product_code,
            "product_name": lab.catalog.product_name(s.product_code),
            "spec_version": s.spec_version,
            "batch_no": s.batch_no,
            "stage": s.stage,
            "status": s.status,
            "registered_by": s.registered_by,
            "registered_at": s.registered_at,
            "assigned_to": s.assigned_to,
            "results": [{
                "parameter": r.parameter,
                "attempt": r.attempt,
                "value": r.value,
                "status": r.status,
                "analyst": r.analyst,
                "instrument_id": r.instrument_id,
                "entered_at": r.entered_at.isoformat(),
                "authorized_retest": r.authorized_retest,
                "inputs": r.inputs,
            } for r in s.results],
            "custody": s.custody,
            "signatures": [{"name": sig.signer_name, "meaning": sig.meaning, "at": sig.timestamp}
                           for sig in s.signatures],
            "spec_parameters": [{
                "name": p.name,
                "unit": p.unit,
                "method": p.method,
                "formula": p.formula,
                "inputs": list(p.inputs),
                "instrument_required": p.instrument_required,
                "low": p.spec.low,
                "high": p.spec.high,
                "decimals": p.spec.decimals,
            } for p in spec.parameters],
        }
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/samples")
def register_sample(req: RegisterSampleRequest):
    try:
        s = lab.lab.register_sample(req.actor_id, req.product_code, req.batch_no, req.stage, req.spec_version)
        return {"id": s.id, "status": s.status}
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/samples/{sample_id}/assign")
def assign_sample(sample_id: str, req: AssignSampleRequest):
    try:
        s = lab.lab.assign(req.actor_id, sample_id, req.analyst_id)
        return {"id": s.id, "status": s.status, "assigned_to": s.assigned_to}
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/samples/{sample_id}/results")
def enter_result(sample_id: str, req: EnterResultRequest):
    try:
        entry = lab.lab.enter_result(req.actor_id, sample_id, req.parameter, req.values, req.instrument_id)
        return {
            "parameter": entry.parameter,
            "attempt": entry.attempt,
            "value": entry.value,
            "status": entry.status,
            "analyst": entry.analyst,
        }
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/samples/{sample_id}/submit")
def submit_sample(sample_id: str, req: SubmitReviewRequest):
    try:
        s = lab.lab.submit_for_review(req.actor_id, sample_id)
        return {"id": s.id, "status": s.status}
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/samples/{sample_id}/approve")
def approve_sample(sample_id: str, req: ApproveSampleRequest):
    try:
        s = lab.lab.approve(req.actor_id, sample_id, req.password)
        return {"id": s.id, "status": s.status}
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/samples/{sample_id}/reject")
def reject_sample(sample_id: str, req: RejectSampleRequest):
    try:
        s = lab.lab.reject(req.actor_id, sample_id, req.password, req.reason)
        return {"id": s.id, "status": s.status}
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/samples/{sample_id}/issue-coa")
def issue_coa(sample_id: str, actor_id: str = Query(...)):
    try:
        coa = lab.lab.issue_coa(actor_id, sample_id)
        return coa
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/coa/verify")
def verify_coa_endpoint(coa: Dict[str, Any]):
    return {"valid": verify_coa(coa)}

@app.get("/api/oos")
def list_investigations(sample_id: Optional[str] = None):
    invs = lab.lab.investigations(sample_id)
    return [{
        "id": i.id,
        "sample_id": i.sample_id,
        "parameter": i.parameter,
        "attempt": i.attempt,
        "value": i.value,
        "opened_at": i.opened_at,
        "status": i.status,
        "retest_authorized": i.retest_authorized,
        "retest_attempt": i.retest_attempt,
        "history": i.history,
    } for i in invs]

@app.post("/api/oos/{oos_id}/phase1")
def conclude_phase1(oos_id: str, req: ConcludePhase1Request):
    try:
        inv = lab.lab.conclude_phase1(req.actor_id, oos_id, req.assignable_cause, req.notes, req.password)
        return {
            "id": inv.id,
            "status": inv.status,
            "retest_authorized": inv.retest_authorized,
        }
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/oos/{oos_id}/phase2")
def conclude_phase2(oos_id: str, req: ConcludePhase2Request):
    try:
        inv = lab.lab.conclude_phase2(req.actor_id, oos_id, req.confirmed, req.notes, req.password)
        return {
            "id": inv.id,
            "status": inv.status,
            "retest_authorized": inv.retest_authorized,
        }
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/api/instruments")
def list_instruments():
    insts = lab.instruments.list_instruments()
    today = clock.now().date()
    return [{
        "instrument_id": i.instrument_id,
        "name": i.name,
        "calibration_due": i.calibration_due.isoformat(),
        "in_service": i.in_service,
        "out_of_service_reason": i.out_of_service_reason,
        "is_calibrated": today <= i.calibration_due,
        "days_remaining": (i.calibration_due - today).days,
    } for i in insts]

@app.post("/api/instruments")
def register_instrument(req: RegisterInstrumentRequest):
    try:
        lab.instruments.register(req.actor_id, req.instrument_id, req.name, req.calibration_due)
        return {"status": "registered", "instrument_id": req.instrument_id}
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/instruments/{instrument_id}/calibrate")
def record_calibration(instrument_id: str, req: CalibrateInstrumentRequest):
    try:
        lab.instruments.record_calibration(req.actor_id, instrument_id, req.calibrated_on, req.next_due)
        return {"status": "calibrated", "instrument_id": instrument_id}
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/instruments/{instrument_id}/out-of-service")
def set_out_of_service(instrument_id: str, req: OutOfServiceRequest):
    try:
        lab.instruments.set_out_of_service(req.actor_id, instrument_id, req.reason)
        return {"status": "out_of_service", "instrument_id": instrument_id}
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/api/sentinel/review")
def run_sentinel(actor_id: str = "qam"):
    try:
        report = lab.sentinel.run(actor_id)
        return {
            "generated_at": report.generated_at,
            "overall_risk": report.overall_risk,
            "chain_intact": report.chain_intact,
            "records_analyzed": report.records_analyzed,
            "entries_analyzed": report.entries_analyzed,
            "scores": report.scores,
            "risk_by_subject": report.risk_by_subject,
            "summary_text": report.summary(),
            "findings": [{
                "code": f.code,
                "severity": f.severity,
                "subject": f.subject,
                "message": f.message,
                "evidence": f.evidence,
            } for f in report.findings],
        }
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.get("/api/audit")
def list_audit(entity_id: Optional[str] = None):
    entries = lab.audit.entries(entity_id)
    return [{
        "seq": e.seq,
        "actor": e.actor,
        "action": e.action,
        "entity_type": e.entity_type,
        "entity_id": e.entity_id,
        "details": e.details,
        "reason": e.reason,
        "timestamp": e.timestamp,
        "prev_hash": e.prev_hash,
        "hash": e.hash,
    } for e in entries]

@app.post("/api/audit/verify")
def verify_audit():
    try:
        valid = lab.audit.verify()
        return {"valid": valid, "count": len(lab.audit.entries())}
    except Exception as exc:
        return {"valid": False, "error": str(exc), "count": len(lab.audit.entries())}

@app.post("/api/audit/simulate-tamper")
def simulate_tamper():
    from dataclasses import replace
    if lab.audit._chain:
        idx = min(2, len(lab.audit._chain) - 1)
        orig = lab.audit._chain[idx]
        lab.audit._chain[idx] = replace(orig, actor="unauthorized_actor")
        return {"status": "tampered", "tampered_index": idx, "seq": orig.seq}
    return {"status": "no_entries"}

@app.get("/api/stability")
def list_stability():
    studies = lab.stability.list_studies()
    today = clock.now().date()
    out = []
    for s in studies:
        sched = lab.stability.schedule(s.id, today)
        out.append({
            "id": s.id,
            "product_code": s.product_code,
            "batch_no": s.batch_no,
            "condition": s.condition,
            "start_date": s.start_date.isoformat(),
            "timepoints": list(s.timepoints),
            "pulls": {str(k): v for k, v in s.pulls.items()},
            "schedule": [{
                "month": r["month"],
                "due_date": r["due_date"].isoformat(),
                "state": r["state"],
            } for r in sched],
        })
    return out

@app.post("/api/stability")
def create_stability_study(req: CreateStabilityStudyRequest):
    try:
        study = lab.stability.create_study(req.actor_id, req.product_code, req.batch_no, req.condition, req.start_date)
        return {"id": study.id}
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/stability/{study_id}/pull")
def record_stability_pull(study_id: str, req: RecordPullRequest):
    try:
        res = lab.stability.record_pull(req.actor_id, study_id, req.month, req.on_date)
        return res
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/api/amv/assess")
def assess_amv(actor_id: str = "ann", method: str = "HPLC-ASSAY-01",
               precision_data: Optional[AmvPrecisionRequest] = None,
               accuracy_data: Optional[AmvAccuracyRequest] = None,
               linearity_data: Optional[AmvLinearityRequest] = None):
    from . import amv
    checks = []
    try:
        if precision_data:
            checks.append(amv.precision(precision_data.values, precision_data.max_rsd))
        if accuracy_data:
            checks.append(amv.accuracy(accuracy_data.pairs, accuracy_data.low, accuracy_data.high))
        if linearity_data:
            checks.append(amv.linearity(linearity_data.x, linearity_data.y, linearity_data.min_r2))
        if not checks:
            raise HTTPException(status_code=400, detail="Provide at least one check")
        report = lab.amv.assess(actor_id, method, checks)
        return report
    except LimsError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

# Serve the UI static files or main dashboard
FRONTEND_HTML_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "lims_ui", "index.html")

@app.get("/", response_class=HTMLResponse)
def root_dashboard():
    if os.path.exists(FRONTEND_HTML_PATH):
        with open(FRONTEND_HTML_PATH, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>LabSentinel LIMS API is running. UI template not found.</h1>"

