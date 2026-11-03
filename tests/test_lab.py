from datetime import date

import pytest

from conftest import EXPIRY, ASSAY_OOS, ASSAY_PASS, PH_PASS, WATER_PASS, build_lab, enter_all, new_sample, to_review
from lims_core.errors import (ComplianceError, EntitlementError, NotFoundError, PermissionDenied,
                              SignatureError, TransitionError, ValidationError)
from lims_core.lab import verify_coa


# ---- registration & assignment --------------------------------------------------
def test_register_sample(ws):
    s = ws.lab.register_sample("ann", "PARA500", "B-001")
    assert s.id == "S-2026-00001" and s.status == "Registered" and s.spec_version == 1
    assert s.custody[0]["event"] == "REGISTERED"


def test_sample_ids_increment(ws):
    ws.lab.register_sample("ann", "PARA500", "B-1")
    assert ws.lab.register_sample("ann", "PARA500", "B-2").id == "S-2026-00002"


def test_registration_validation_and_roles(ws):
    with pytest.raises(PermissionDenied):
        ws.lab.register_sample("rev", "PARA500", "B-1")
    with pytest.raises(ValidationError):
        ws.lab.register_sample("ann", "PARA500", "  ")
    with pytest.raises(ValidationError):
        ws.lab.register_sample("ann", "PARA500", "B-1", stage="mystery")
    with pytest.raises(NotFoundError):
        ws.lab.register_sample("ann", "UNKNOWN", "B-1")


def test_assignment_rules(ws):
    s = ws.lab.register_sample("ann", "PARA500", "B-1")
    with pytest.raises(PermissionDenied, match="analyst"):
        ws.lab.assign("ann", s.id, "rev")
    out = ws.lab.assign("ann", s.id, "ali")
    assert out.status == "InTesting" and out.assigned_to == "ali"
    assert out.custody[-1]["event"] == "ASSIGNED"
    ws.lab.assign("qam", s.id, "ann")  # reassignment is fine before any results exist


def test_cannot_reassign_after_results(ws):
    sid = new_sample(ws)
    ws.lab.enter_result("ann", sid, "pH", PH_PASS)
    with pytest.raises(TransitionError):
        ws.lab.assign("qam", sid, "ali")


# ---- result entry ---------------------------------------------------------------
def test_enter_calculated_result(ws):
    sid = new_sample(ws)
    r = ws.lab.enter_result("ann", sid, "Assay", ASSAY_PASS, "HPLC-01")
    assert r.value == 100.0 and r.status == "PASS" and r.attempt == 1 and not r.authorized_retest


def test_result_needs_registered_state_and_assigned_analyst(ws):
    s = ws.lab.register_sample("ann", "PARA500", "B-1")
    with pytest.raises(TransitionError):
        ws.lab.enter_result("ann", s.id, "pH", PH_PASS)
    ws.lab.assign("ann", s.id, "ann")
    with pytest.raises(PermissionDenied, match="assigned"):
        ws.lab.enter_result("ali", s.id, "pH", PH_PASS)
    with pytest.raises(PermissionDenied):
        ws.lab.enter_result("rev", s.id, "pH", PH_PASS)


def test_instrument_required_and_must_be_calibrated(ws, clock):
    sid = new_sample(ws)
    with pytest.raises(ValidationError, match="requires an instrument"):
        ws.lab.enter_result("ann", sid, "Assay", ASSAY_PASS)
    clock.set(clock.now().replace(year=2026, month=8, day=1))
    with pytest.raises(ComplianceError, match="calibration expired"):
        ws.lab.enter_result("ann", sid, "Assay", ASSAY_PASS, "HPLC-01")
    assert ws.lab.get_sample(sid).results == []


def test_invalid_inputs_are_rejected(ws):
    sid = new_sample(ws)
    with pytest.raises(ValidationError):
        ws.lab.enter_result("ann", sid, "Assay", {"sample_area": 1}, "HPLC-01")
    with pytest.raises(ValidationError):
        ws.lab.enter_result("ann", sid, "pH", {"result": "6"})
    with pytest.raises(NotFoundError):
        ws.lab.enter_result("ann", sid, "Colour", {"result": 1})
    with pytest.raises(NotFoundError):
        ws.lab.enter_result("ann", "S-0", "pH", PH_PASS)


def test_passing_result_cannot_be_retested(ws):
    sid = new_sample(ws)
    ws.lab.enter_result("ann", sid, "pH", PH_PASS)
    with pytest.raises(ComplianceError, match="passing result already exists"):
        ws.lab.enter_result("ann", sid, "pH", {"result": 6.1})


def test_results_are_audited_with_values(ws):
    sid = new_sample(ws)
    ws.lab.enter_result("ann", sid, "pH", PH_PASS)
    e = ws.audit.entries(sid)[-1]
    assert e.action == "RESULT_ENTERED" and e.details["value"] == 6.0 and e.actor == "ann"


# ---- review ----------------------------------------------------------------------
def test_submit_requires_all_parameters(ws):
    sid = new_sample(ws)
    ws.lab.enter_result("ann", sid, "pH", PH_PASS)
    with pytest.raises(ComplianceError, match="without a final result"):
        ws.lab.submit_for_review("ann", sid)


def test_only_assigned_analyst_submits(ws):
    sid = new_sample(ws)
    enter_all(ws, sid)
    with pytest.raises(PermissionDenied):
        ws.lab.submit_for_review("ali", sid)


def test_approve_flow_and_signature(ws):
    sid = to_review(ws)
    s = ws.lab.approve("rev", sid, "pw-rev")
    assert s.status == "Approved" and s.signatures[0].meaning == "Approved"
    assert s.signatures[0].signer_name == "Rita Reviewer"


def test_bad_signature_blocks_and_is_audited(ws):
    sid = to_review(ws)
    with pytest.raises(SignatureError):
        ws.lab.approve("rev", sid, "wrong")
    assert ws.lab.get_sample(sid).status == "UnderReview"
    assert ws.audit.entries(sid)[-1].action == "SIGNATURE_FAILED"


def test_analyst_cannot_review_own_work(ws):
    sid = new_sample(ws, actor="dual")
    enter_all(ws, sid, actor="dual")
    ws.lab.submit_for_review("dual", sid)
    with pytest.raises(PermissionDenied, match="Segregation"):
        ws.lab.approve("dual", sid, "pw-dual")


def test_wrong_role_cannot_approve(ws):
    sid = to_review(ws)
    with pytest.raises(PermissionDenied):
        ws.lab.approve("ali", sid, "pw-ali")


def test_reject_needs_reason(ws):
    sid = to_review(ws)
    with pytest.raises(ValidationError):
        ws.lab.reject("rev", sid, "pw-rev", " ")
    s = ws.lab.reject("rev", sid, "pw-rev", "Transcription error suspected")
    assert s.status == "Rejected"
    assert ws.audit.entries(sid)[-1].reason == "Transcription error suspected"


def test_review_only_from_under_review(ws):
    sid = new_sample(ws)
    with pytest.raises(TransitionError):
        ws.lab.approve("rev", sid, "pw-rev")


# ---- Certificate of Analysis --------------------------------------------------------
def test_coa_requires_approval(ws):
    sid = to_review(ws)
    with pytest.raises(ComplianceError):
        ws.lab.issue_coa("rev", sid)


def test_coa_content_and_hash(ws):
    sid = to_review(ws)
    ws.lab.approve("rev", sid, "pw-rev")
    coa = ws.lab.issue_coa("rev", sid)
    assert coa["coa_number"] == f"COA-{sid}-1" and coa["product_name"].startswith("Paracetamol")
    assert [r["parameter"] for r in coa["results"]] == ["Assay", "pH", "Water"]
    assert coa["results"][0]["method"] == "HPLC-ASSAY-01" and coa["approved_by"][0]["name"] == "Rita Reviewer"
    assert verify_coa(coa)


def test_tampered_coa_is_detected(ws):
    sid = to_review(ws)
    ws.lab.approve("rev", sid, "pw-rev")
    coa = ws.lab.issue_coa("rev", sid)
    coa["results"][0]["value"] = 101.0
    assert not verify_coa(coa)


def test_coa_reissue_increments(ws):
    sid = to_review(ws)
    ws.lab.approve("rev", sid, "pw-rev")
    ws.lab.issue_coa("rev", sid)
    assert ws.lab.issue_coa("qam", sid)["coa_number"].endswith("-2")


def test_coa_roles(ws):
    sid = to_review(ws)
    ws.lab.approve("rev", sid, "pw-rev")
    with pytest.raises(PermissionDenied):
        ws.lab.issue_coa("ann", sid)


# ---- tenancy & subscription ----------------------------------------------------------
def test_labs_are_isolated(platform, ws):
    other = build_lab(platform, "lab2")
    sid = new_sample(ws)
    with pytest.raises(NotFoundError):
        other.lab.get_sample(sid)
    assert other.lab.list_samples() == [] and other.audit.entries(sid) == ()
    # a user of lab1 does not exist in lab3 unless created there
    lab3 = platform.onboard("lab3", "basic", EXPIRY)
    with pytest.raises(PermissionDenied):
        lab3.lab.register_sample("ann", "PARA500", "B-1")


def test_audit_chains_are_independent(platform, ws):
    other = build_lab(platform, "lab2")
    new_sample(ws)
    assert ws.audit.verify() and other.audit.verify()
    assert len(ws.audit.entries()) != len(other.audit.entries())


def test_expired_subscription_freezes_lab(ws, clock):
    clock.advance(days=400)
    with pytest.raises(EntitlementError, match="expired"):
        ws.lab.register_sample("ann", "PARA500", "B-1")


def test_list_samples_by_status(ws):
    new_sample(ws, batch="B-1")
    ws.lab.register_sample("ann", "PARA500", "B-2")
    assert len(ws.lab.list_samples("Registered")) == 1 and len(ws.lab.list_samples()) == 2


def test_returned_samples_are_copies(ws):
    sid = new_sample(ws)
    s = ws.lab.get_sample(sid)
    s.status = "Approved"
    assert ws.lab.get_sample(sid).status == "InTesting"


def test_coa_reports_value_rounded_to_spec_decimals(ws):
    sid = new_sample(ws)
    ws.lab.enter_result("ann", sid, "Assay", {**ASSAY_PASS, "std_area": 990, "purity": 99.5}, "HPLC-01")
    ws.lab.enter_result("ann", sid, "pH", PH_PASS)
    ws.lab.enter_result("ann", sid, "Water", WATER_PASS)
    ws.lab.submit_for_review("ann", sid)
    ws.lab.approve("rev", sid, "pw-rev")
    raw = ws.lab.get_sample(sid).results[0].value
    assert raw == pytest.approx(100.505, rel=1e-3) and raw != 100.5
    assert ws.lab.issue_coa("rev", sid)["results"][0]["value"] == 100.5
