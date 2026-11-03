from datetime import date

import pytest

from lims_core import amv
from lims_core.errors import ComplianceError, EntitlementError, NotFoundError, PermissionDenied, ValidationError
from lims_core.stability import ICH_ACCELERATED, add_months

START = date(2026, 1, 5)


# ---- stability ---------------------------------------------------------------------
@pytest.mark.parametrize("start,n,expected", [
    (date(2026, 1, 31), 1, date(2026, 2, 28)), (date(2024, 1, 31), 1, date(2024, 2, 29)),
    (date(2026, 11, 30), 3, date(2027, 2, 28)), (date(2026, 1, 5), 12, date(2027, 1, 5)),
    (date(2026, 12, 15), 1, date(2027, 1, 15)), (date(2026, 3, 31), 0, date(2026, 3, 31))])
def test_add_months(start, n, expected):
    assert add_months(start, n) == expected


@pytest.fixture
def study(ws):
    return ws.stability.create_study("qam", "PARA500", "B-001", "30C/65%RH", START)


def test_study_creation(ws, study):
    assert study.id == "STB-0001" and study.timepoints == (0, 3, 6, 9, 12, 18, 24, 36)
    assert ws.audit.entries(study.id)[0].action == "STABILITY_STUDY_CREATED"


def test_study_validation(ws):
    with pytest.raises(ValidationError, match="condition"):
        ws.stability.create_study("qam", "PARA500", "B", "20C/20%RH", START)
    with pytest.raises(ValidationError, match="month 0"):
        ws.stability.create_study("qam", "PARA500", "B", "25C/60%RH", START, timepoints=(3, 6))
    with pytest.raises(NotFoundError):
        ws.stability.create_study("qam", "NOPE", "B", "25C/60%RH", START)
    with pytest.raises(PermissionDenied):
        ws.stability.create_study("rev", "PARA500", "B", "25C/60%RH", START)


def test_schedule_states(ws, study):
    sched = {r["month"]: r for r in ws.stability.schedule(study.id, date(2026, 4, 20))}
    assert sched[0]["state"] == "overdue" and sched[3]["state"] == "overdue"
    assert sched[6]["state"] == "upcoming" and sched[3]["due_date"] == date(2026, 4, 5)
    first = {r["month"]: r["state"] for r in ws.stability.schedule(study.id, START)}
    assert first[0] == "due" and first[3] == "upcoming"


def test_pulls_needing_action_sorted(ws, study):
    s2 = ws.stability.create_study("qam", "PARA500", "B-002", "25C/60%RH", date(2026, 2, 1),
                                   ICH_ACCELERATED)
    out = ws.stability.pulls_needing_action(date(2026, 2, 3))
    assert [(r["study_id"], r["month"]) for r in out] == [("STB-0001", 0), ("STB-0002", 0)]
    assert out[0]["state"] == "overdue" and out[1]["state"] == "overdue"
    assert s2.id == "STB-0002"


def test_record_pull_on_time_early_late_and_duplicate(ws, study):
    assert ws.stability.record_pull("ann", study.id, 0, START)["late"] is False
    with pytest.raises(ComplianceError, match="already"):
        ws.stability.record_pull("ann", study.id, 0, START)
    with pytest.raises(ComplianceError, match="Too early"):
        ws.stability.record_pull("ann", study.id, 3, date(2026, 3, 20))
    assert ws.stability.record_pull("ann", study.id, 3, date(2026, 4, 5))["late"] is False
    assert ws.stability.record_pull("ann", study.id, 6, date(2026, 7, 20))["late"] is True
    states = {r["month"]: r["state"] for r in ws.stability.schedule(study.id, date(2026, 7, 20))}
    assert states[0] == states[3] == states[6] == "pulled"
    assert ws.audit.entries(study.id)[-1].details["late"] is True


def test_pull_validation(ws, study):
    with pytest.raises(ValidationError):
        ws.stability.record_pull("ann", study.id, 4, START)
    with pytest.raises(NotFoundError):
        ws.stability.record_pull("ann", "STB-9999", 0, START)


def test_stability_needs_plan(platform):
    from conftest import build_lab
    basic = build_lab(platform, "basic-lab", plan="basic")
    with pytest.raises(EntitlementError, match="stability"):
        basic.stability.create_study("qam", "PARA500", "B", "25C/60%RH", START)


# ---- AMV --------------------------------------------------------------------------------
def test_precision():
    reps = [100.1, 99.8, 100.3, 99.9, 100.0, 100.2]
    ok = amv.precision(reps, 2.0)
    assert ok.passed and ok.value == pytest.approx(0.1871, abs=1e-3)
    assert not amv.precision(reps, 0.1).passed
    with pytest.raises(ValidationError):
        amv.precision([1.0], 2)
    with pytest.raises(ValidationError):
        amv.precision([-1.0, 1.0], 2)


def test_accuracy():
    ok = amv.accuracy([(80, 79.5), (100, 100.4), (120, 119.0)])
    assert ok.passed and ok.value == pytest.approx(99.65, abs=0.01)
    assert not amv.accuracy([(80, 79.5), (100, 95.0)]).passed
    with pytest.raises(ValidationError):
        amv.accuracy([])
    with pytest.raises(ValidationError):
        amv.accuracy([(0, 1)])


def test_linearity():
    x = [50, 75, 100, 125, 150]
    perfect = amv.linearity(x, [2.5, 3.75, 5.0, 6.25, 7.5])
    assert perfect.passed and perfect.value == pytest.approx(1.0)
    assert perfect.details["slope"] == pytest.approx(0.05)
    assert not amv.linearity(x, [2.5, 3.2, 5.9, 6.0, 7.5]).passed
    with pytest.raises(ValidationError):
        amv.linearity([1, 2, 3], [1, 2, 3])
    with pytest.raises(ValidationError):
        amv.linearity([1, 1, 1, 1, 1], [1, 2, 3, 4, 5])


def test_lod_loq():
    out = amv.lod_loq(0.02, 0.05)
    assert out["LOD"] == pytest.approx(1.32) and out["LOQ"] == pytest.approx(4.0)
    with pytest.raises(ValidationError):
        amv.lod_loq(0.1, 0)


def test_amv_service(ws):
    checks = [amv.precision([100, 100.2, 99.9, 100.1], 2), amv.accuracy([(100, 100.5)])]
    report = ws.amv.assess("ann", "HPLC assay v1", checks)
    assert report["overall"] == "PASS" and len(report["checks"]) == 2
    failing = ws.amv.assess("qam", "HPLC assay v1", checks + [amv.accuracy([(100, 90)])])
    assert failing["overall"] == "FAIL"
    assert ws.audit.entries("HPLC assay v1")[0].action == "AMV_ASSESSED"
    with pytest.raises(ValidationError):
        ws.amv.assess("ann", "m", [])
    with pytest.raises(PermissionDenied):
        ws.amv.assess("rev", "m", checks)
