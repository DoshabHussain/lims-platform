"""Runnable walkthrough: a normal release test, an OOS investigation, then suspicious activity
that the Data Integrity Sentinel picks up.   Run:  python demo.py"""
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, "src")

from lims_core.clock import FixedClock
from lims_core.config import ParameterSpec
from lims_core.container import LimsPlatform
from lims_core.formula import Specification
from lims_core.lab import verify_coa

clock = FixedClock(datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc))  # a Monday
platform = LimsPlatform(clock)
lab = platform.onboard("demo-lab", "enterprise", datetime(2027, 1, 1, tzinfo=timezone.utc))
for uid, name, roles in [("ann", "Ann Analyst", {"analyst"}), ("ali", "Ali Analyst", {"analyst"}),
                         ("rev", "Rita Reviewer", {"reviewer"}), ("qam", "Qasim QA", {"qa_manager"}),
                         ("adm", "Adnan Admin", {"admin"})]:
    lab.users.add_user(uid, name, "pw-" + uid, roles)

lab.catalog.define_product("adm", "PARA500", "Paracetamol 500 mg Tablets")
lab.catalog.publish_spec("adm", "PARA500", [
    ParameterSpec("Assay", "%", Specification(95, 105, 1), method="HPLC-01",
                  formula="(sample_area / std_area) * (std_weight / sample_weight) * purity",
                  instrument_required=True),
    ParameterSpec("pH", "", Specification(5.5, 6.5, 1)),
    ParameterSpec("Water", "%", Specification(high=0.5, decimals=2)),
])
lab.instruments.register("adm", "HPLC-01", "HPLC System 1", date(2026, 12, 31))

ok = {"sample_area": 1000, "std_area": 1000, "std_weight": 50, "sample_weight": 50, "purity": 100.0}


def sample(analyst, batch):
    s = lab.lab.register_sample(analyst, "PARA500", batch)
    lab.lab.assign(analyst, s.id, analyst)
    return s.id


print("== 1. Normal release test ==")
sid = sample("ann", "B-001")
lab.lab.enter_result("ann", sid, "Assay", ok, "HPLC-01")
lab.lab.enter_result("ann", sid, "pH", {"result": 6.0})
lab.lab.enter_result("ann", sid, "Water", {"result": 0.2})
lab.lab.submit_for_review("ann", sid)
lab.lab.approve("rev", sid, "pw-rev")
coa = lab.lab.issue_coa("rev", sid)
print(f"{coa['coa_number']} issued, hash verified: {verify_coa(coa)}")

print("\n== 2. OOS result: retest is blocked until investigated ==")
sid2 = sample("ann", "B-002")
lab.lab.enter_result("ann", sid2, "Assay", {**ok, "sample_area": 1100}, "HPLC-01")
try:
    lab.lab.enter_result("ann", sid2, "Assay", ok, "HPLC-01")
except Exception as exc:
    print("Blocked:", exc)
inv = lab.lab.investigations(sid2)[0]
lab.lab.conclude_phase1("qam", inv.id, True, "Dilution error found", "pw-qam")
print("Retest after investigation:", lab.lab.enter_result("ann", sid2, "Assay", ok, "HPLC-01").status)

print("\n== 3. Suspicious activity (weekend, identical values, failed signatures) ==")
clock.set(datetime(2026, 1, 10, 10, 0, tzinfo=timezone.utc))  # Saturday
for i in range(6):
    s = sample("ali", f"B-1{i}")
    lab.lab.enter_result("ali", s, "Assay", ok, "HPLC-01")
    lab.lab.enter_result("ali", s, "pH", {"result": 6.123})      # same value every time
    lab.lab.enter_result("ali", s, "Water", {"result": 0.2})
    clock.advance(seconds=8)
review = lab.lab.submit_for_review("ali", s)
for _ in range(3):
    try:
        lab.lab.approve("rev", s, "guess")
    except Exception:
        pass

print(lab.sentinel.run("qam").summary())
