from datetime import datetime, timezone

import pytest

from conftest import EXPIRY, USERS, build_lab
from lims_core.container import LimsPlatform
from lims_core.entitlements import EntitlementService
from lims_core.clock import FixedClock
from lims_core.errors import EntitlementError, NotFoundError, ValidationError


def test_unknown_plan_and_missing_subscription():
    ent = EntitlementService(FixedClock())
    with pytest.raises(EntitlementError, match="Unknown plan"):
        ent.subscribe("t", "platinum", EXPIRY)
    for call in (lambda: ent.plan_for("t"), lambda: ent.cancel("t"), lambda: ent.assert_module("t", "lims")):
        with pytest.raises(EntitlementError, match="No subscription"):
            call()


def test_cancelled_subscription_blocks_work(platform, ws):
    platform.entitlements.cancel("lab1")
    with pytest.raises(EntitlementError, match="cancelled"):
        ws.lab.register_sample("ann", "PARA500", "B-1")


def test_plans_gate_modules(platform):
    basic = build_lab(platform, "basic-lab", plan="basic")
    pro = build_lab(platform, "pro-lab", plan="professional")
    assert basic.lab.register_sample("ann", "PARA500", "B-1").id
    for name, call in {"stability": lambda w: w.stability.create_study("qam", "PARA500", "B", "25C/60%RH", datetime(2026, 1, 1).date()),
                       "amv": lambda w: w.amv.assess("ann", "m", [object()])}.items():
        with pytest.raises(EntitlementError, match=name):
            call(basic)
    with pytest.raises(EntitlementError, match="sentinel"):
        pro.sentinel.run("qam")


def test_upgrade_unlocks_modules(platform):
    ws = build_lab(platform, "grow", plan="basic")
    platform.entitlements.subscribe("grow", "enterprise", EXPIRY)
    assert ws.sentinel.run("qam").overall_risk == "NONE"


def test_basic_plan_user_limit(platform):
    ws = platform.onboard("small", "basic", EXPIRY)
    for i in range(10):
        ws.users.add_user(f"u{i}", "U", "pw", {"analyst"})
    with pytest.raises(EntitlementError, match="User limit"):
        ws.users.add_user("u10", "U", "pw", {"analyst"})


def test_onboarding_rules(platform):
    build_lab(platform, "lab-x")
    with pytest.raises(ValidationError, match="already exists"):
        platform.onboard("lab-x", "basic", EXPIRY)
    with pytest.raises(NotFoundError):
        platform.workspace("ghost")
    assert platform.workspace("lab-x").tenant_id == "lab-x"
