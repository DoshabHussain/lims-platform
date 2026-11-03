from dataclasses import replace

import pytest

from lims_core.audit import GENESIS_HASH, AuditTrail
from lims_core.clock import FixedClock
from lims_core.errors import EntitlementError, IntegrityError, PermissionDenied, SignatureError, ValidationError
from lims_core.formula import Formula, Specification, round_half_up
from lims_core.identity import ESigner, UserDirectory


# ---- audit ------------------------------------------------------------------
@pytest.fixture
def trail():
    return AuditTrail("t1", FixedClock())


def test_audit_chain_links(trail):
    a = trail.append("u", "X", "e", "1")
    b = trail.append("u", "Y", "e", "1")
    assert a.prev_hash == GENESIS_HASH and b.prev_hash == a.hash and b.seq == 2


def test_audit_verifies_and_detects_changes(trail):
    e = trail.append("u", "X", "e", "1", {"v": 1})
    trail.append("u", "X", "e", "1", {"v": 2})
    assert trail.verify()
    trail._chain[0] = replace(e, details={"v": 9})
    with pytest.raises(IntegrityError, match="modified"):
        trail.verify()


def test_audit_detects_deletion_and_copies_details(trail):
    d = {"k": [1]}
    for i in range(3):
        trail.append("u", "X", "e", str(i), d)
    d["k"].append(2)
    assert trail.entries()[0].details == {"k": [1]}
    del trail._chain[1]
    with pytest.raises(IntegrityError):
        trail.verify()


def test_audit_filter_by_entity(trail):
    trail.append("u", "X", "e", "A")
    trail.append("u", "X", "e", "B")
    assert [e.entity_id for e in trail.entries("B")] == ["B"]


# ---- formula / rounding / specification ---------------------------------------
def test_assay_formula():
    f = Formula("(sample_area / std_area) * (std_weight / sample_weight) * purity")
    assert f.evaluate({"sample_area": 1000, "std_area": 990, "std_weight": 50,
                       "sample_weight": 50, "purity": 99.5}) == pytest.approx(100.505, rel=1e-3)


def test_formula_variables_and_functions():
    assert Formula("a + b * 2").variables == {"a", "b"}
    assert Formula("round(a / b, 2)").evaluate({"a": 1, "b": 3}) == 0.33
    assert Formula("-a + 2 * 3").evaluate({"a": 1}) == 5


def test_formula_errors():
    with pytest.raises(ValidationError, match="Missing"):
        Formula("a + b").evaluate({"a": 1})
    with pytest.raises(ValidationError, match="zero"):
        Formula("a / b").evaluate({"a": 1, "b": 0})
    with pytest.raises(ValidationError, match="number"):
        Formula("a + 1").evaluate({"a": "5"})
    with pytest.raises(ValidationError, match="number"):
        Formula("a + 1").evaluate({"a": True})
    with pytest.raises(ValidationError, match="Invalid"):
        Formula("a +")


@pytest.mark.parametrize("evil", ["__import__('os').system('ls')", "open('x')", "a ** 9999999",
                                  "[1]", "a.__class__", "(lambda: 1)()", "'t'", "a if b else c"])
def test_unsafe_formulas_rejected(evil):
    with pytest.raises(ValidationError):
        Formula(evil)


@pytest.mark.parametrize("value,decimals,expected", [
    (2.5, 0, 3.0), (3.5, 0, 4.0), (102.05, 1, 102.1), (0.125, 2, 0.13), (1.004, 2, 1.0), (-2.5, 0, -3.0)])
def test_round_half_up(value, decimals, expected):
    assert round_half_up(value, decimals) == expected


def test_specification_rounds_before_comparing():
    spec = Specification(95.0, 105.0, 1)
    assert spec.evaluate(105.04) == "PASS"   # rounds to 105.0
    assert spec.evaluate(105.05) == "OOS"    # rounds to 105.1
    assert spec.evaluate(94.95) == "PASS"    # rounds to 95.0
    assert spec.evaluate(94.94) == "OOS"


def test_specification_without_rounding_and_one_sided():
    assert Specification(98, 102).evaluate(102.01) == "OOS"
    assert Specification(high=0.5).evaluate(0.3) == "PASS"


def test_invalid_specifications():
    for kwargs in ({}, {"low": 10, "high": 5}, {"low": 1, "decimals": -1}):
        with pytest.raises(ValidationError):
            Specification(**kwargs)


# ---- identity -----------------------------------------------------------------
def test_passwords_and_signatures():
    users = UserDirectory("t1")
    users.add_user("a", "Alice", "secret", {"x"})
    assert users.verify_password("a", "secret") and not users.verify_password("a", "bad")
    assert not users.verify_password("ghost", "secret")
    sig = ESigner(users, FixedClock()).sign("a", "secret", "Approved", "S-1")
    assert (sig.signer_name, sig.meaning) == ("Alice", "Approved")
    with pytest.raises(SignatureError):
        ESigner(users, FixedClock()).sign("a", "bad", "Approved", "S-1")


def test_unknown_user_and_user_limit():
    users = UserDirectory("t1", max_users=1)
    users.add_user("a", "A", "pw", set())
    with pytest.raises(EntitlementError):
        users.add_user("b", "B", "pw", set())
    with pytest.raises(PermissionDenied):
        users.get("ghost")
