from datetime import date, datetime, timezone
import math
import pytest

from conftest import ASSAY_OOS, ASSAY_PASS, PH_PASS, build_lab, new_sample
from lims_core import amv
from lims_core.config import ParameterSpec
from lims_core.errors import NotFoundError, ValidationError
from lims_core.formula import Formula, Specification, round_half_up


def test_nan_and_inf_in_specification_rejected():
    spec = Specification(95.0, 105.0, 1)
    with pytest.raises(ValidationError, match="finite"):
        spec.evaluate(float("nan"))
    with pytest.raises(ValidationError, match="finite"):
        spec.evaluate(float("inf"))
    with pytest.raises(ValidationError, match="finite"):
        spec.evaluate(float("-inf"))

    spec_no_dec = Specification(95.0, 105.0)
    with pytest.raises(ValidationError, match="finite"):
        spec_no_dec.evaluate(float("nan"))


def test_nan_and_inf_in_round_half_up():
    with pytest.raises(ValidationError, match="non-finite"):
        round_half_up(float("nan"), 2)
    with pytest.raises(ValidationError, match="non-finite"):
        round_half_up(float("inf"), 1)


def test_nan_and_inf_in_formula():
    f = Formula("a + b")
    with pytest.raises(ValidationError, match="finite"):
        f.evaluate({"a": float("nan"), "b": 10.0})
    with pytest.raises(ValidationError, match="finite"):
        f.evaluate({"a": float("inf"), "b": 10.0})


def test_amv_linearity_constant_response():
    with pytest.raises(ValidationError, match="Responses must vary"):
        amv.linearity([1.0, 2.0, 3.0, 4.0, 5.0], [10.0, 10.0, 10.0, 10.0, 10.0])


def test_amv_lod_loq_negative_sd():
    with pytest.raises(ValidationError, match="negative"):
        amv.lod_loq(-1.0, 2.0)


def test_stability_study_empty_batch_no(ws):
    with pytest.raises(ValidationError, match="Batch number is required"):
        ws.stability.create_study("qam", "PARA500", "   ", "25C/60%RH", date(2026, 1, 1))


def test_catalog_listing_methods(ws):
    products = ws.catalog.list_products()
    assert "PARA500" in products
    specs = ws.catalog.list_specs("PARA500")
    assert len(specs) >= 1
    with pytest.raises(NotFoundError):
        ws.catalog.list_specs("GHOST")


def test_instrument_listing_and_get(ws):
    insts = ws.instruments.list_instruments()
    assert any(i.instrument_id == "HPLC-01" for i in insts)
    inst = ws.instruments.get_instrument("HPLC-01")
    assert inst.instrument_id == "HPLC-01"
    assert inst.in_service is True
    with pytest.raises(NotFoundError):
        ws.instruments.get_instrument("UNKNOWN")


def test_user_directory_list_users(ws):
    users = ws.users.list_users()
    uids = {u.user_id for u in users}
    assert "ann" in uids and "qam" in uids and "rev" in uids


def test_stability_listing_and_get(ws):
    study = ws.stability.create_study("qam", "PARA500", "B-999", "25C/60%RH", date(2026, 1, 1))
    studies = ws.stability.list_studies()
    assert any(s.id == study.id for s in studies)
    loaded = ws.stability.get_study(study.id)
    assert loaded.id == study.id
    with pytest.raises(NotFoundError):
        ws.stability.get_study("STB-GHOST")


def test_lab_get_investigation(ws):
    sid = new_sample(ws)
    ws.lab.enter_result("ann", sid, "Assay", ASSAY_OOS, "HPLC-01")
    invs = ws.lab.investigations(sid)
    assert len(invs) == 1
    loaded = ws.lab.get_investigation(invs[0].id)
    assert loaded.id == invs[0].id
    with pytest.raises(NotFoundError):
        ws.lab.get_investigation("OOS-9999")

