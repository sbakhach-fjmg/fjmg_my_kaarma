import base64
import logging

import httpx
import pytest

from mykaarma import cli, config
from mykaarma.client import MPI_SCOPE, ApiError, ConfigError, MyKaarma

USER, PW = "user-x", "pw-y-secret"
B64 = base64.b64encode(f"{USER}:{PW}".encode()).decode()

CONFIGS = {
    "statusCode": 200,
    "enabledBusinesses": [{
        "uuid": "biz-1", "name": "FJ Newport",
        "departments": [
            {"uuid": "dept-svc", "name": "Service", "enabledDepartmentLevelScopes": [MPI_SCOPE, "order.read"]},
            {"uuid": "dept-parts", "name": "Parts", "enabledDepartmentLevelScopes": ["order.read"]},
        ],
    }],
}

# Shapes as seen live (Nov 2026): the VIN filter returns summaries with mpiSections null; line items need the detail call.
SUMMARIES = {
    "statusCode": 200,
    "mpisForOrdersMap": {"order-1": [{
        "uuid": "mpi-1", "mpiSections": None, "recommendedTotal": 612.5, "approvedTotal": 0.0,
        "orderIdentifiers": {"orderUUID": "order-1", "orderNumber": "R123", "orderDate": "2026-10-22", "orderStatus": "C"},
    }]},
}
DETAIL = {
    "statusCode": 200,
    "multiPointInsepction": {"uuid": "mpi-1", "mpiSections": [{"name": "Tires", "checks": [
        {"name": "Front tires", "laborOpCode": "TIRE2",
         "fieldList": [{"state": "green"}, {"state": "red"}, {"state": "gray"}],
         "recommendations": [
             {"description": None, "status": "incomplete", "approvalState": None, "amount": {"amount": 612.5, "unit": "USD"}},
             {"description": None, "status": None, "amount": None}]},   # empty template slot: dropped
        {"name": "Rear tires", "fieldList": [{"state": "gray"}], "recommendations": [{"status": None}]},
    ]}]},
}


def mpi_api(req):
    if req.url.path.endswith("/multiPointInspections"):
        return httpx.Response(200, json=SUMMARIES)
    if req.url.path.endswith("/order/order-1/multiPointInspection/mpi-1"):
        return httpx.Response(200, json=DETAIL)
    return httpx.Response(404)


def mk(handler) -> MyKaarma:
    return MyKaarma(USER, PW, transport=httpx.MockTransport(handler))


def test_sends_basic_auth_and_parses_departments():
    seen = {}

    def handler(req):
        seen["auth"] = req.headers["authorization"]
        seen["path"] = req.url.path
        return httpx.Response(200, json=CONFIGS)

    rows = mk(handler).check_access()
    assert seen == {"auth": f"Basic {B64}", "path": "/developer/application/configurations"}
    assert [r["department_uuid"] for r in rows] == ["dept-svc", "dept-parts"]
    assert rows[0]["business"] == "FJ Newport" and MPI_SCOPE in rows[0]["scopes"]


def test_recommendations_from_summary_then_detail_dropping_empty_slots():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        assert "WDD" not in str(req.url)
        return mpi_api(req)

    recs = mk(handler).recommended_work("WDDZF4JB1HA123456", "dept-svc")
    assert calls == [("POST", "/scprocessor/mpi/v1/dealerDepartment/dept-svc/multiPointInspections"),
                     ("GET", "/scprocessor/mpi/v1/dealerDepartment/dept-svc/order/order-1/multiPointInspection/mpi-1")]
    assert recs == [{
        "order_uuid": "order-1", "order_number": "R123", "order_date": "2026-10-22", "order_status": "C",
        "recommended_total": 612.5, "approved_total": 0.0, "section": "Tires", "check": "Front tires",
        "description": "", "severity": "red", "status": "incomplete", "approval_state": "",
        "amount": 612.5, "op_code": "TIRE2",
    }]


def test_vin_goes_in_the_body():
    bodies = []

    def handler(req):
        if req.method == "POST":
            bodies.append(req.content)
        return mpi_api(req)

    mk(handler).inspections("WDDZF4JB1HA123456", "dept-svc")
    assert bodies == [b'{"vin":"WDDZF4JB1HA123456"}']


@pytest.mark.parametrize("status", [401, 403, 500])
def test_errors_never_contain_credentials(status, caplog):
    caplog.set_level(logging.DEBUG)
    client = mk(lambda req: httpx.Response(status, json={"echo": req.headers["authorization"]}))
    with pytest.raises(ApiError) as e:
        client.check_access()
    text = f"{e.value} {e.value!r} {client!r} {caplog.text}"
    for secret in (PW, B64, USER):
        assert secret not in text
    if status in (401, 403):
        assert "rejected the credentials" in str(e.value)


def test_transport_failure_hides_request():
    def handler(req):
        raise httpx.ConnectError(f"boom {req.headers['authorization']}", request=req)

    with pytest.raises(ApiError) as e:
        mk(handler).check_access()
    assert B64 not in str(e.value) and e.value.__cause__ is None and e.value.__suppress_context__


def test_missing_credentials_raise_before_any_request():
    with pytest.raises(ConfigError):
        MyKaarma("", PW, transport=httpx.MockTransport(lambda req: pytest.fail("network call made")))


def test_fan_out_skips_unscoped_and_survives_a_failing_department():
    depts = [
        {"business": "A", "department": "Service", "department_uuid": "ok", "scopes": [MPI_SCOPE]},
        {"business": "B", "department": "Parts", "department_uuid": "noscope", "scopes": ["order.read"]},
        {"business": "C", "department": "Service", "department_uuid": "broken", "scopes": [MPI_SCOPE]},
    ]
    called = []

    def handler(req):
        uuid = req.url.path.split("/")[5]
        called.append(uuid)
        return httpx.Response(500) if uuid == "broken" else mpi_api(req)

    results = cli.fetch_all(mk(handler), "WDDZF4JB1HA123456", depts)
    assert called == ["ok", "ok", "broken"]
    assert len(results[0][1]) == 1
    assert results[1][1] is None and "skipped" in results[1][2]
    assert results[2][1] is None and "500" in results[2][2]


def test_check_cli_output_has_no_secrets(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MYKAARMA_USERNAME", USER)
    monkeypatch.setenv("MYKAARMA_PASSWORD", PW)
    monkeypatch.setattr(config, "DEPARTMENTS_FILE", tmp_path / "departments.json")
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(cli, "_client", lambda: mk(lambda req: httpx.Response(200, json=CONFIGS)))
    assert cli.main(["check", "--save"]) == 0
    out = capsys.readouterr().out
    assert "credentials: set" in out and "dept-svc" in out
    for secret in (PW, B64, USER):
        assert secret not in out
    saved = (tmp_path / "departments.json").read_text()
    assert "dept-parts" in saved and PW not in saved and USER not in saved


def test_default_picks_service_departments_only():
    depts = [{"business": b, "department": d, "department_uuid": f"{b}-{d}"}
             for b in ("A", "B") for d in ("Service", "Parts", "Sales")]
    assert [d["department_uuid"] for d in cli._pick(depts, None)] == ["A-Service", "B-Service"]
    assert len(cli._pick(depts, None, all_depts=True)) == 6
    assert [d["department_uuid"] for d in cli._pick(depts, "b parts")] == ["B-Parts"]
