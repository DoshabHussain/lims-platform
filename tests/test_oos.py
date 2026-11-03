import pytest

from conftest import ASSAY_OOS, ASSAY_PASS, PH_PASS, WATER_PASS, new_sample, to_review
from lims_core.errors import ComplianceError, PermissionDenied, SignatureError, TransitionError, ValidationError


@pytest.fixture
def oos(ws):
    sid = new_sample(ws)
    r = ws.lab.enter_result("ann", sid, "Assay", ASSAY_OOS, "HPLC-01")
    assert r.status == "OOS"
    return sid, ws.lab.investigations(sid)[0]


def test_oos_opens_an_investigation(ws, oos):
    sid, inv = oos
    assert inv.id == "OOS-0001" and inv.status == "Phase I" and inv.value == pytest.approx(110.0)
    assert ws.audit.entries(inv.id)[0].action == "OOS_OPENED"


def test_retest_blocked_while_investigating(ws, oos):
    sid, _ = oos
    with pytest.raises(ComplianceError, match="Retest not authorised"):
        ws.lab.enter_result("ann", sid, "Assay", ASSAY_PASS, "HPLC-01")


def test_cannot_submit_with_open_oos(ws, oos):
    sid, _ = oos
    ws.lab.enter_result("ann", sid, "pH", PH_PASS)
    ws.lab.enter_result("ann", sid, "Water", WATER_PASS)
    with pytest.raises(ComplianceError, match="Assay"):
        ws.lab.submit_for_review("ann", sid)


def test_assignable_cause_authorises_exactly_one_retest(ws, oos):
    sid, inv = oos
    out = ws.lab.conclude_phase1("qam", inv.id, True, "Wrong dilution", "pw-qam")
    assert out.status == "Invalidated" and out.retest_authorized
    r = ws.lab.enter_result("ann", sid, "Assay", ASSAY_PASS, "HPLC-01")
    assert (r.attempt, r.status, r.authorized_retest) == (2, "PASS", True)
    after = ws.lab.investigations(sid)[0]
    assert not after.retest_authorized and after.retest_attempt == 2
    with pytest.raises(ComplianceError):
        ws.lab.enter_result("ann", sid, "Assay", ASSAY_PASS, "HPLC-01")


def test_failed_retest_opens_new_investigation(ws, oos):
    sid, inv = oos
    ws.lab.conclude_phase1("qam", inv.id, True, "Suspected error", "pw-qam")
    ws.lab.enter_result("ann", sid, "Assay", ASSAY_OOS, "HPLC-01")
    invs = ws.lab.investigations(sid)
    assert [i.id for i in invs] == ["OOS-0001", "OOS-0002"] and invs[1].attempt == 2
    with pytest.raises(ComplianceError):
        ws.lab.enter_result("ann", sid, "Assay", ASSAY_PASS, "HPLC-01")


def test_no_assignable_cause_escalates_to_phase2(ws, oos):
    _, inv = oos
    out = ws.lab.conclude_phase1("qam", inv.id, False, "No lab error found", "pw-qam")
    assert out.status == "Phase II" and not out.retest_authorized


def test_phase2_invalidated_authorises_retest(ws, oos):
    sid, inv = oos
    ws.lab.conclude_phase1("qam", inv.id, False, "No lab error", "pw-qam")
    out = ws.lab.conclude_phase2("qam", inv.id, False, "Process cause: sampling", "pw-qam")
    assert out.status == "Invalidated" and out.retest_authorized
    assert ws.lab.enter_result("ann", sid, "Assay", ASSAY_PASS, "HPLC-01").attempt == 2


def test_confirmed_oos_means_sample_cannot_be_approved(ws, oos):
    sid, inv = oos
    ws.lab.conclude_phase1("qam", inv.id, False, "No lab error", "pw-qam")
    ws.lab.conclude_phase2("qam", inv.id, True, "Confirmed batch failure", "pw-qam")
    with pytest.raises(ComplianceError):
        ws.lab.enter_result("ann", sid, "Assay", ASSAY_PASS, "HPLC-01")  # no testing into compliance
    ws.lab.enter_result("ann", sid, "pH", PH_PASS)
    ws.lab.enter_result("ann", sid, "Water", WATER_PASS)
    ws.lab.submit_for_review("ann", sid)
    with pytest.raises(ComplianceError, match="confirmed OOS"):
        ws.lab.approve("rev", sid, "pw-rev")
    assert ws.lab.reject("rev", sid, "pw-rev", "Confirmed OOS").status == "Rejected"
    with pytest.raises(ComplianceError):
        ws.lab.issue_coa("rev", sid)


def test_investigator_must_be_independent(ws):
    sid = new_sample(ws, actor="ann")
    ws.users.add_user("both", "Both", "pw-both", {"analyst", "qa_manager"})
    s2 = ws.lab.register_sample("both", "PARA500", "B-9")
    ws.lab.assign("both", s2.id, "both")
    ws.lab.enter_result("both", s2.id, "Assay", ASSAY_OOS, "HPLC-01")
    inv = ws.lab.investigations(s2.id)[0]
    with pytest.raises(PermissionDenied, match="independent"):
        ws.lab.conclude_phase1("both", inv.id, True, "notes", "pw-both")


def test_investigation_rules(ws, oos):
    _, inv = oos
    with pytest.raises(PermissionDenied):
        ws.lab.conclude_phase1("rev", inv.id, True, "x", "pw-rev")
    with pytest.raises(ValidationError):
        ws.lab.conclude_phase1("qam", inv.id, True, "  ", "pw-qam")
    with pytest.raises(SignatureError):
        ws.lab.conclude_phase1("qam", inv.id, True, "ok", "wrong")
    with pytest.raises(TransitionError):
        ws.lab.conclude_phase2("qam", inv.id, True, "ok", "pw-qam")  # still Phase I
    ws.lab.conclude_phase1("qam", inv.id, True, "ok", "pw-qam")
    with pytest.raises(TransitionError):
        ws.lab.conclude_phase1("qam", inv.id, True, "again", "pw-qam")
