from datetime import datetime, timedelta, timezone

import pytest

from conftest import ASSAY_PASS, new_sample, to_review
from lims_core import sentinel as sn
from lims_core.audit import AuditTrail
from lims_core.clock import FixedClock
from lims_core.errors import EntitlementError, PermissionDenied, SignatureError

CFG = sn.SentinelConfig()
MON_10 = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)  # Monday 15:00 local (UTC+5)


def rec(**kw):
    base = dict(sample_id="S1", parameter="Assay", attempt=1, value=100.0, status="PASS",
                analyst="ann", instrument_id="HPLC-01", entered_at=MON_10,
                spec_low=95.0, spec_high=105.0, decimals=1, authorized_retest=False)
    base.update(kw)
    return sn.ResultRecord(**base)


def codes(findings):
    return [f.code for f in findings]


# ---- testing into compliance ----------------------------------------------------------
def test_unauthorised_pass_after_oos_is_flagged():
    out = sn.detect_testing_into_compliance(
        [rec(attempt=1, status="OOS", value=90.0), rec(attempt=2, status="PASS")])
    assert codes(out) == ["TESTING_INTO_COMPLIANCE"] and out[0].severity == "HIGH"
    assert out[0].subject == "ann"


def test_authorised_retest_is_not_flagged():
    out = sn.detect_testing_into_compliance(
        [rec(attempt=1, status="OOS", value=90.0), rec(attempt=2, authorized_retest=True)])
    assert out == []


def test_many_authorised_attempts_flag_excessive_retesting():
    items = [rec(attempt=1, status="OOS"), rec(attempt=2, status="OOS", authorized_retest=True),
             rec(attempt=3, status="PASS", authorized_retest=True)]
    assert codes(sn.detect_testing_into_compliance(items)) == ["EXCESSIVE_RETESTING"]


def test_single_attempts_are_clean():
    assert sn.detect_testing_into_compliance([rec(sample_id="S1"), rec(sample_id="S2")]) == []


# ---- clustering at limits ----------------------------------------------------------------
def edge_records(n_edge, n_mid):
    vals = [104.8] * n_edge + [100.0] * n_mid
    return [rec(sample_id=f"S{i}", value=v) for i, v in enumerate(vals)]


def test_clustering_flagged():
    out = sn.detect_limit_clustering(edge_records(8, 2), CFG)
    assert codes(out) == ["RESULTS_CLUSTER_AT_LIMIT"] and out[0].evidence["ratio"] == 0.8


def test_clustering_below_ratio_or_sample_size_is_clean():
    assert sn.detect_limit_clustering(edge_records(3, 7), CFG) == []
    assert sn.detect_limit_clustering(edge_records(5, 0), CFG) == []  # n < 10


def test_clustering_ignores_oos_and_one_sided_specs():
    oos = [rec(sample_id=f"S{i}", value=104.8, status="OOS") for i in range(12)]
    one_sided = [rec(sample_id=f"S{i}", value=0.49, spec_low=None) for i in range(12)]
    assert sn.detect_limit_clustering(oos + one_sided, CFG) == []


# ---- duplicate values ----------------------------------------------------------------------
def test_duplicate_values_across_samples_flagged():
    out = sn.detect_duplicate_values([rec(sample_id=f"S{i}", value=99.873) for i in range(3)], CFG)
    assert codes(out) == ["DUPLICATE_RESULTS"] and out[0].severity == "HIGH"
    assert out[0].evidence["samples"] == ["S0", "S1", "S2"]


def test_low_precision_repeats_and_same_sample_are_clean():
    coarse = [rec(sample_id=f"S{i}", value=100.0) for i in range(5)]
    same_sample = [rec(sample_id="S1", attempt=i, value=99.873) for i in range(4)]
    two_only = [rec(sample_id=f"S{i}", value=99.873) for i in range(2)]
    assert sn.detect_duplicate_values(coarse + same_sample + two_only, CFG) == []


# ---- terminal digit bias ----------------------------------------------------------------------
def test_uniform_last_digits_are_clean():
    items = [rec(sample_id=f"S{i}", value=100.0 + (i % 10) / 10) for i in range(100)]
    assert sn.detect_terminal_digit_bias(items, CFG) == []


def test_digit_preference_is_flagged():
    items = [rec(sample_id=f"S{i}", value=100.0 + (0.5 if i % 2 else 0.0)) for i in range(100)]
    out = sn.detect_terminal_digit_bias(items, CFG)
    assert codes(out) == ["TERMINAL_DIGIT_BIAS"] and out[0].evidence["chi2"] == pytest.approx(400.0)


def test_digit_check_needs_enough_data_and_decimals():
    few = [rec(sample_id=f"S{i}", value=100.5) for i in range(30)]
    no_decimals = [rec(sample_id=f"S{i}", value=100.5, decimals=None) for i in range(80)]
    assert sn.detect_terminal_digit_bias(few + no_decimals, CFG) == []


# ---- off-hours & rapid entry -------------------------------------------------------------------
def at(**kw):
    return MON_10.replace(**kw)


def test_weekend_entries_flagged():
    sat = datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc)
    out = sn.detect_off_hours_entry([rec(sample_id=f"S{i}", entered_at=sat) for i in range(5)], CFG)
    assert codes(out) == ["OFF_HOURS_ENTRY"]


def test_working_hour_boundaries_use_local_time():
    inside = [rec(sample_id=f"S{i}", entered_at=at(hour=13, minute=59)) for i in range(5)]  # 18:59 local
    outside = [rec(sample_id=f"S{i}", entered_at=at(hour=14, minute=0)) for i in range(5)]  # 19:00 local
    assert sn.detect_off_hours_entry(inside, CFG) == []
    assert codes(sn.detect_off_hours_entry(outside, CFG)) == ["OFF_HOURS_ENTRY"]


def test_offset_can_be_configured_and_small_samples_ignored():
    items = [rec(sample_id=f"S{i}", entered_at=at(hour=14)) for i in range(5)]
    assert sn.detect_off_hours_entry(items, sn.SentinelConfig(utc_offset_hours=0)) == []
    assert sn.detect_off_hours_entry(items[:3], CFG) == []


def test_rapid_entry_flagged_only_across_different_samples():
    fast = [rec(sample_id=f"S{i}", entered_at=MON_10 + timedelta(seconds=5 * i)) for i in range(5)]
    slow = [rec(sample_id=f"S{i}", entered_at=MON_10 + timedelta(seconds=90 * i)) for i in range(5)]
    same = [rec(sample_id="S1", attempt=i, entered_at=MON_10 + timedelta(seconds=i)) for i in range(5)]
    assert codes(sn.detect_rapid_entry(fast, CFG)) == ["IMPLAUSIBLY_FAST_ENTRY"]
    assert sn.detect_rapid_entry(slow, CFG) == [] and sn.detect_rapid_entry(same, CFG) == []


# ---- audit-trail review ---------------------------------------------------------------------------
def trail_with(*actions, gap_minutes=0):
    clock = FixedClock()
    t = AuditTrail("t1", clock)
    for actor, action, reason in actions:
        t.append(actor, action, "sample", "S1", {}, reason)
        clock.advance(minutes=gap_minutes)
    return t.entries()


def test_repeated_signature_failures_flagged():
    entries = trail_with(*[("rev", "SIGNATURE_FAILED", None)] * 3, gap_minutes=2)
    out = sn.review_audit_trail(entries, CFG)
    assert codes(out) == ["REPEATED_SIGNATURE_FAILURES"] and out[0].subject == "rev"


def test_spread_out_failures_and_other_actors_are_clean():
    spread = trail_with(*[("rev", "SIGNATURE_FAILED", None)] * 3, gap_minutes=20)
    mixed = trail_with(("a", "SIGNATURE_FAILED", None), ("b", "SIGNATURE_FAILED", None),
                       ("c", "SIGNATURE_FAILED", None))
    assert sn.review_audit_trail(spread, CFG) == [] and sn.review_audit_trail(mixed, CFG) == []


def test_critical_action_without_reason_flagged():
    entries = trail_with(("qam", "OOS_PHASE1", None), ("qam", "REJECT", "  "), ("qam", "REJECT", "ok"),
                         ("qam", "APPROVE", None))
    out = sn.review_audit_trail(entries, CFG)
    assert codes(out) == ["MISSING_REASON", "MISSING_REASON"]


# ---- scoring ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("score,level", [(0, "NONE"), (1, "LOW"), (3, "LOW"), (4, "MEDIUM"),
                                         (9, "MEDIUM"), (10, "HIGH"), (25, "HIGH")])
def test_risk_levels(score, level):
    assert sn.risk_level(score) == level


def test_analyze_scores_per_subject():
    items = [rec(attempt=1, status="OOS", value=90.0, analyst="ann"),
             rec(attempt=2, status="PASS", analyst="ann")]
    report = sn.analyze(items, [])
    assert report.scores == {"ann": 10} and report.risk_by_subject["ann"] == "HIGH"
    assert report.overall_risk == "HIGH" and report.chain_intact


def test_analyze_clean_data():
    report = sn.analyze([rec(sample_id="S1"), rec(sample_id="S2", value=99.2)], [])
    assert report.overall_risk == "NONE" and report.findings == () and report.records_analyzed == 2


def test_broken_chain_is_a_high_finding():
    report = sn.analyze([], [], chain_intact=False)
    assert codes(report.findings) == ["AUDIT_CHAIN_BROKEN"] and report.overall_risk == "HIGH"


def test_summary_text_mentions_findings():
    report = sn.analyze([rec(sample_id=f"S{i}", value=99.873) for i in range(3)], [])
    text = report.summary()
    assert "overall risk HIGH" in text and "DUPLICATE_RESULTS" in text and "ann: HIGH" in text


# ---- end to end through the service ---------------------------------------------------------------------
def test_clean_lab_activity_has_no_findings(ws):
    to_review(ws)
    report = ws.sentinel.run("qam")
    assert report.overall_risk == "NONE" and report.chain_intact
    assert ws.audit.entries()[-1].action == "SENTINEL_RUN"


def test_failed_signatures_through_the_service_are_detected(ws):
    sid = to_review(ws)
    for _ in range(3):
        with pytest.raises(SignatureError):
            ws.lab.approve("rev", sid, "wrong")
    report = ws.sentinel.run("qam")
    assert any(f.code == "REPEATED_SIGNATURE_FAILURES" and f.subject == "rev" for f in report.findings)
    assert report.risk_by_subject["rev"] == "HIGH"


def test_tampering_with_the_audit_trail_is_surfaced(ws):
    to_review(ws)
    from dataclasses import replace
    ws.audit._chain[3] = replace(ws.audit._chain[3], actor="mallory")
    report = ws.sentinel.run("qam")
    assert not report.chain_intact and "AUDIT_CHAIN_BROKEN" in codes(report.findings)
    assert report.overall_risk == "HIGH"


def test_weekend_activity_through_the_service_is_detected(ws, clock):
    clock.set(datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc))  # Saturday
    for i in range(2):
        sid = new_sample(ws, batch=f"B-{i}")
        ws.lab.enter_result("ann", sid, "Assay", ASSAY_PASS, "HPLC-01")
        ws.lab.enter_result("ann", sid, "pH", {"result": 6.0})
        ws.lab.enter_result("ann", sid, "Water", {"result": 0.2})
    report = ws.sentinel.run("qam")
    assert "OFF_HOURS_ENTRY" in codes(report.findings)


def test_sentinel_access_control_and_plan(ws, platform):
    from conftest import build_lab
    with pytest.raises(PermissionDenied):
        ws.sentinel.run("ann")
    pro = build_lab(platform, "pro-lab", plan="professional")
    with pytest.raises(EntitlementError, match="sentinel"):
        pro.sentinel.run("qam")


def test_result_records_export(ws):
    to_review(ws)
    records = ws.lab.result_records()
    assert len(records) == 3
    assay = next(r for r in records if r.parameter == "Assay")
    assert (assay.spec_low, assay.spec_high, assay.decimals, assay.instrument_id) == (95.0, 105.0, 1, "HPLC-01")
