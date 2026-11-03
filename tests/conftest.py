from datetime import date, datetime, timezone

import pytest

from lims_core import identity
from lims_core.clock import FixedClock
from lims_core.config import ParameterSpec
from lims_core.container import LimsPlatform
from lims_core.formula import Specification

identity._ITERATIONS = 1000  # fast password hashing for tests only

EXPIRY = datetime(2027, 1, 1, tzinfo=timezone.utc)

USERS = [
    ("ann", "Ann Analyst", {"analyst"}),
    ("ali", "Ali Analyst", {"analyst"}),
    ("rev", "Rita Reviewer", {"reviewer"}),
    ("qam", "Qasim QA", {"qa_manager"}),
    ("adm", "Adnan Admin", {"admin"}),
    ("dual", "Dana Dual", {"analyst", "reviewer"}),
]

ASSAY_PASS = {"sample_area": 1000, "std_area": 1000, "std_weight": 50, "sample_weight": 50, "purity": 100.0}
ASSAY_OOS = {**ASSAY_PASS, "sample_area": 1100}  # 110.0 %
PH_PASS = {"result": 6.0}
WATER_PASS = {"result": 0.2}


def para_params():
    return [
        ParameterSpec("Assay", "%", Specification(95.0, 105.0, 1),
                      formula="(sample_area / std_area) * (std_weight / sample_weight) * purity",
                      method="HPLC-ASSAY-01", instrument_required=True),
        ParameterSpec("pH", "", Specification(5.5, 6.5, 1)),
        ParameterSpec("Water", "%", Specification(high=0.5, decimals=2)),
    ]


def build_lab(platform, tenant="lab1", plan="enterprise"):
    ws = platform.onboard(tenant, plan, EXPIRY)
    for uid, name, roles in USERS:
        ws.users.add_user(uid, name, "pw-" + uid, roles)
    ws.catalog.define_product("adm", "PARA500", "Paracetamol 500 mg Tablets")
    ws.catalog.publish_spec("adm", "PARA500", para_params())
    ws.instruments.register("adm", "HPLC-01", "HPLC System 1", date(2026, 6, 30))
    return ws


def enter_all(ws, sample_id, actor="ann"):
    ws.lab.enter_result(actor, sample_id, "Assay", ASSAY_PASS, "HPLC-01")
    ws.lab.enter_result(actor, sample_id, "pH", PH_PASS)
    ws.lab.enter_result(actor, sample_id, "Water", WATER_PASS)


def new_sample(ws, actor="ann", batch="B-001"):
    s = ws.lab.register_sample(actor, "PARA500", batch)
    ws.lab.assign(actor, s.id, actor)
    return s.id


def to_review(ws, actor="ann", batch="B-001"):
    sid = new_sample(ws, actor, batch)
    enter_all(ws, sid, actor)
    ws.lab.submit_for_review(actor, sid)
    return sid


@pytest.fixture
def clock():
    return FixedClock()


@pytest.fixture
def platform(clock):
    return LimsPlatform(clock)


@pytest.fixture
def ws(platform):
    return build_lab(platform)
