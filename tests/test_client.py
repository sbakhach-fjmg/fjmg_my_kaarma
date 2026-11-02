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

MPIS = {
    "statusCode": 200,
    "mpisForOrdersMap": {"order-1": [{"orderNumber": "R123", "mpiSections": [{"name": "Tires", "checks": [
        {"name": "Front tires", "description": "Tread depth", "recommendations": [
            {"description": "Replace front tires", "approvalState": "DECLINED", "status": "OPEN",
             "amount": {"amount": 612.5, "unit": "USD"}, "opCodes": ["TIRE2"]}]},
        {"name": "Rear tires", "recommendations": []},
    ]}]}]},
}


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


def test_recommendations_flattened_and_vin_in_body_not_url():
    def handler(req):
        assert req.url.path == "/scprocessor/mpi/v1/dealerDepartment/dept-svc/multiPointInspections"
        assert "WDD" not in str(req.url)
        assert b'"vin"' in req.content
        return httpx.Response(200, json=MPIS)

    recs = mk(handler).recommended_work("WDDZF4JB1HA123456", "dept-svc")
    assert recs == [{
        "order_uuid": "order-1", "order_number": "R123", "section": "Tires", "check": "Front tires",
        "description": "Replace front tires", "approval_state": "DECLINED", "status": "OPEN", "work_status": "",
        "amount": 612.5, "op_codes": ["TIRE2"], "created_on": "",
    }]


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
        return httpx.Response(500) if uuid == "broken" else httpx.Response(200, json=MPIS)

    results = cli.fetch_all(mk(handler), "WDDZF4JB1HA123456", depts)
    assert called == ["ok", "broken"]
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
