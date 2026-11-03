from datetime import date

import pytest

from conftest import para_params
from lims_core.config import ParameterSpec
from lims_core.errors import ComplianceError, NotFoundError, PermissionDenied, ValidationError
from lims_core.formula import Specification


def test_parameter_inputs():
    p = para_params()
    assert p[0].inputs == ("purity", "sample_area", "sample_weight", "std_area", "std_weight")
    assert p[1].inputs == ("result",)


def test_parameter_compute_and_validation():
    assert para_params()[1].compute({"result": 6}) == 6.0
    with pytest.raises(ValidationError):
        para_params()[1].compute({"result": "6"})
    with pytest.raises(ValidationError):
        ParameterSpec("X", "", Specification(1, 2), formula="a +")
    with pytest.raises(ValidationError):
        ParameterSpec(" ", "", Specification(1, 2))


def test_spec_versions_are_immutable(ws):
    v1 = ws.catalog.get_spec("PARA500")
    ws.catalog.publish_spec("qam", "PARA500", [ParameterSpec("pH", "", Specification(6.0, 7.0, 1))])
    assert v1.version == 1 and ws.catalog.get_spec("PARA500").version == 2
    assert len(ws.catalog.get_spec("PARA500", 1).parameters) == 3
    with pytest.raises(NotFoundError):
        ws.catalog.get_spec("PARA500", 9)


def test_samples_pin_their_spec_version(ws):
    s = ws.lab.register_sample("ann", "PARA500", "B-1")
    ws.catalog.publish_spec("qam", "PARA500", [ParameterSpec("pH", "", Specification(6.0, 7.0, 1))])
    assert ws.lab.get_sample(s.id).spec_version == 1
    assert ws.lab.register_sample("ann", "PARA500", "B-2").spec_version == 2


def test_catalog_validation_and_roles(ws):
    with pytest.raises(ValidationError):
        ws.catalog.publish_spec("adm", "PARA500", [])
    p = para_params()[1]
    with pytest.raises(ValidationError, match="Duplicate"):
        ws.catalog.publish_spec("adm", "PARA500", [p, p])
    with pytest.raises(ValidationError, match="already exists"):
        ws.catalog.define_product("adm", "PARA500", "x")
    with pytest.raises(NotFoundError):
        ws.catalog.publish_spec("adm", "NOPE", [p])
    with pytest.raises(PermissionDenied):
        ws.catalog.define_product("ann", "NEW", "New")
    with pytest.raises(NotFoundError):
        ws.catalog.get_spec("PARA500").parameter("Colour")


# ---- instruments ---------------------------------------------------------------
def test_instrument_in_calibration_is_usable(ws):
    ws.instruments.assert_usable("HPLC-01", date(2026, 6, 30))


def test_expired_calibration_blocks_use(ws):
    with pytest.raises(ComplianceError, match="calibration expired"):
        ws.instruments.assert_usable("HPLC-01", date(2026, 7, 1))


def test_recalibration_restores_use(ws):
    ws.instruments.record_calibration("adm", "HPLC-01", date(2026, 7, 1), date(2027, 7, 1))
    ws.instruments.assert_usable("HPLC-01", date(2026, 12, 1))


def test_out_of_service_blocks_until_calibrated(ws):
    ws.instruments.set_out_of_service("qam", "HPLC-01", "Pump leak")
    with pytest.raises(ComplianceError, match="out of service"):
        ws.instruments.assert_usable("HPLC-01", date(2026, 1, 5))
    ws.instruments.record_calibration("qam", "HPLC-01", date(2026, 1, 6), date(2026, 12, 31))
    ws.instruments.assert_usable("HPLC-01", date(2026, 1, 7))


def test_instrument_validation_and_roles(ws):
    with pytest.raises(PermissionDenied):
        ws.instruments.register("ann", "X", "X", date(2027, 1, 1))
    with pytest.raises(ValidationError):
        ws.instruments.register("adm", "HPLC-01", "dup", date(2027, 1, 1))
    with pytest.raises(ValidationError):
        ws.instruments.record_calibration("adm", "HPLC-01", date(2026, 5, 1), date(2026, 5, 1))
    with pytest.raises(ValidationError):
        ws.instruments.set_out_of_service("adm", "HPLC-01", "  ")
    with pytest.raises(NotFoundError):
        ws.instruments.assert_usable("GHOST", date(2026, 1, 1))


def test_instrument_changes_are_audited(ws):
    ws.instruments.set_out_of_service("adm", "HPLC-01", "Service")
    last = ws.audit.entries("HPLC-01")[-1]
    assert last.action == "INSTRUMENT_OUT_OF_SERVICE" and last.reason == "Service"
