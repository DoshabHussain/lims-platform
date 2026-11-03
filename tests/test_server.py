from lims_core.server import (
    get_system_status,
    list_samples,
    list_products,
    list_instruments,
    list_stability,
    list_audit,
    run_sentinel,
    verify_audit,
    root_dashboard,
)

def test_server_endpoints_direct():
    status = get_system_status()
    assert status["tenant_id"] == "demo-lab"
    assert status["stats"]["samples_total"] >= 3

    samples = list_samples()
    assert len(samples) >= 3

    products = list_products()
    assert len(products) >= 2

    instruments = list_instruments()
    assert len(instruments) >= 3

    stability = list_stability()
    assert len(stability) >= 1

    audit = list_audit()
    assert len(audit) >= 5

    sentinel = run_sentinel("qam")
    assert "overall_risk" in sentinel
    assert sentinel["records_analyzed"] >= 3

    audit_ver = verify_audit()
    assert audit_ver["valid"] is True

    html = root_dashboard()
    assert "LabSentinel LIMS" in html
