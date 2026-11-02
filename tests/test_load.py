import json
import re
from datetime import datetime, timedelta

import httpx
import pytest

from mykaarma import config, extract, load
from mykaarma.client import MPI_SCOPE, ApiError, MyKaarma

VIN = "WDDZF4JB1HA123456"
NOW = datetime(2026, 11, 2, 12, 0, 0)

SUMMARY = {"uuid": "mpi-1", "status": "Completed", "recommendedTotal": 900.0, "approvedTotal": 600.0,
           "createdOn": 1761100000000, "mpiSections": None,
           "orderIdentifiers": {"orderUUID": "order-1", "orderNumber": "R123", "orderDate": "2026-10-22", "orderStatus": "C"},
           "mpiCounts": {"totalRecommendationCount": 3, "approveCount": 1, "declineCount": 1},
           "customerIdentifiers": {"customerName": "Pat Customer", "customerUUID": "cust-1"},
           "dealerAssociateIdentifiers": {"dealerAssociateUUID": "sa-1", "dealerAssociateName": "Sam Advisor"},
           "technicianIdentifiers": [{"dealerAssociateUUID": "t-1", "dealerAssociateName": "Tess Tech"},
                                     {"dealerAssociateUUID": "t-2", "dealerAssociateName": "Theo Tech"}],
           "assignees": [{"dealerAssociateUUID": "t-1", "dealerAssociateName": "Tess Tech"}]}
DETAIL = {**SUMMARY, "mpiSections": [{"name": "Tires", "checks": [
    {"name": "Front tires", "laborOpCode": "TIRE2", "fieldList": [{"state": "green"}, {"state": "red"}],
     "recommendations": [
         {"uuid": "r-1", "status": "approved", "approvalState": "approved", "approverName": "Sam Advisor",
          "amount": {"amount": 600.0, "unit": "USD"}, "createdOn": 1761100000000,
          "lastUpdatedBy": {"dealerAssociateUUID": "sa-1", "dealerAssociateName": "Sam Advisor"}},
         {"uuid": "slot", "status": None}]},
    {"name": "Wipers", "fieldList": [{"state": "yellow"}], "recommendations": [
        {"uuid": "r-2", "status": "incomplete", "approvalState": "declined", "amount": 300.0}]},
    {"name": "Brakes", "fieldList": [{"state": "red"}], "recommendations": [{"uuid": "r-3", "status": "incomplete"}]},
]}]}


def test_extract_rows():
    insp, recs, assoc = extract.rows(VIN, "FJ Newport", "dept-svc", SUMMARY, DETAIL, NOW)
    assert (insp["mpi_uuid"], insp["order_number"], str(insp["order_date"]), insp["mk_recommended_total"]) == \
        ("mpi-1", "R123", "2026-10-22", 900.0)
    assert insp["mpi_created_on"] == datetime(2025, 10, 22, 2, 26, 40)
    raw = json.loads(insp["raw"])
    assert "customerIdentifiers" not in raw and raw["dealerAssociateIdentifiers"]["dealerAssociateName"] == "Sam Advisor"
    assert "Pat Customer" not in insp["raw"]
    assert [(r["recommendation_uuid"], r["severity"], r["severity_rank"], r["decision"], r["amount"]) for r in recs] == [
        ("r-1", "red", 3, "approved", 600.0), ("r-2", "yellow", 2, "declined", 300.0), ("r-3", "red", 3, "pending", None)]
    assert recs[0]["op_code"] == "TIRE2" and recs[0]["last_updated_by"] == "Sam Advisor" and recs[0]["approver_name"] == "Sam Advisor"
    assert [(a["role"], a["associate_uuid"]) for a in assoc] == [
        ("advisor", "sa-1"), ("technician", "t-1"), ("technician", "t-2"), ("assignee", "t-1")]


@pytest.mark.parametrize("approval,status,want", [
    ("approved", "incomplete", "approved"), ("DECLINED", None, "declined"), (None, "approved", "approved"),
    (None, "declined", "declined"), (None, "incomplete", "pending"), (None, None, "pending")])
def test_decision(approval, status, want):
    assert extract.decision({"approvalState": approval, "status": status}) == want


def test_retry_then_success_and_give_up():
    seq = iter([httpx.Response(429), httpx.Response(200, json={"enabledBusinesses": []})])
    sleeps = []
    assert MyKaarma("u", "p", transport=httpx.MockTransport(lambda r: next(seq)), sleep=sleeps.append).check_access() == []
    assert sleeps == [1]
    calls = []
    mk = MyKaarma("u", "p", transport=httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(503)), sleep=lambda s: None)
    with pytest.raises(ApiError, match="503"):
        mk.check_access()
    assert len(calls) == 3


def test_400_is_not_retried():
    calls = []
    mk = MyKaarma("u", "p", transport=httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(400)), sleep=lambda s: None)
    with pytest.raises(ApiError):
        mk.check_access()
    assert calls == [1]


def api(fail_dept: str | None = None):
    def handler(req):
        dept = req.url.path.split("/")[5]
        if dept == fail_dept:
            return httpx.Response(500)
        if req.url.path.endswith("/multiPointInspections"):
            body = json.loads(req.content)
            found = dept == "d-a" and body["vin"] == VIN
            return httpx.Response(200, json={"mpisForOrdersMap": {"order-1": [SUMMARY]} if found else {}})
        return httpx.Response(200, json={"multiPointInsepction": DETAIL})
    return lambda: MyKaarma("u", "p", transport=httpx.MockTransport(handler), sleep=lambda s: None)


DEPTS = [{"business": "A", "department": "Service", "department_uuid": "d-a", "scopes": [MPI_SCOPE]},
         {"business": "B", "department": "Service", "department_uuid": "d-b", "scopes": [MPI_SCOPE]}]
CARS = [{"vin": VIN, "stock_no": "S1", "dealer_code": "NBMBN"}, {"vin": "1HGCM82633A004352", "stock_no": "S2", "dealer_code": "BHMBN"}]


class FakeDb:
    """Same contract as db.Mssql: errors keep the old rows, success replaces them."""
    def __init__(self):
        self.lookups, self.rows, self.calls = {}, {}, []

    def fresh_vins(self, hours, now):
        return {v for v, l in self.lookups.items() if l["error"] is None and l["checked_at"] >= now - timedelta(hours=hours)}

    def replace_vin(self, lookup, inspections, recs, associates):
        self.calls.append(lookup["vin"])
        if lookup["error"]:
            self.lookups[lookup["vin"]] = {**self.lookups.get(lookup["vin"], {}), **{k: v for k, v in lookup.items() if k != "inspections_found"}}
        else:
            self.lookups[lookup["vin"]] = lookup
            self.rows[lookup["vin"]] = (inspections, recs, associates)

    def prune(self, keep):
        gone = [v for v in self.lookups if v not in keep]
        for v in gone:
            self.lookups.pop(v); self.rows.pop(v, None)
        return len(gone)


def test_load_writes_each_vin_once_and_vins_without_inspections_still_get_a_lookup():
    db = FakeDb()
    stats = load.run(CARS, DEPTS, db, api(), workers=2, log=lambda s: None)
    assert sorted(db.calls) == sorted(c["vin"] for c in CARS)
    assert stats["checked"] == 2 and stats["errors"] == 0 and stats["inspections"] == 1 and stats["recommendations"] == 3
    assert db.lookups[VIN]["inspections_found"] == 1 and db.lookups[VIN]["departments_checked"] == 2
    assert db.lookups["1HGCM82633A004352"]["inspections_found"] == 0
    assert db.rows[VIN][0][0]["store"] == "A"


def test_failing_department_keeps_previous_rows_and_records_error():
    db = FakeDb()
    load.run(CARS[:1], DEPTS, db, api(), workers=1, log=lambda s: None)
    before = db.rows[VIN]
    stats = load.run(CARS[:1], DEPTS, db, api(fail_dept="d-b"), workers=1, skip_fresh_hours=0, log=lambda s: None)
    assert stats["errors"] == 1
    assert db.rows[VIN] is before
    assert "1 department(s) failed" in db.lookups[VIN]["error"] and "500" in db.lookups[VIN]["error"]
    assert db.lookups[VIN]["inspections_found"] == 1   # from the last good check


def test_skip_fresh_and_errored_vins_are_retried():
    db = FakeDb()
    load.run(CARS, DEPTS, db, api(fail_dept="d-b"), workers=1, log=lambda s: None)
    db.calls.clear()
    load.run(CARS, DEPTS, db, api(), workers=1, log=lambda s: None)
    assert sorted(db.calls) == sorted(c["vin"] for c in CARS)       # both errored last time, so both retried
    db.calls.clear()
    stats = load.run(CARS, DEPTS, db, api(), workers=1, log=lambda s: None)
    assert db.calls == [] and stats["skipped"] == 2


def test_prune_only_on_request():
    db = FakeDb()
    load.run(CARS, DEPTS, db, api(), workers=1, log=lambda s: None)
    load.run(CARS[:1], DEPTS, db, api(), workers=1, skip_fresh_hours=0, prune=True, log=lambda s: None)
    assert list(db.lookups) == [VIN]


def test_unexpected_payload_fails_the_vin_not_the_run(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(extract, "rows", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    stats = load.run(CARS, DEPTS, db, api(), workers=2, log=lambda s: None)
    assert stats["checked"] == 2 and stats["errors"] == 1
    assert db.lookups[VIN]["error"] == "unexpected KeyError while extracting"


def test_progress_output_has_no_secrets():
    lines = []
    user, pw = "user-x", "pw-y-secret"
    make = lambda: MyKaarma(user, pw, transport=httpx.MockTransport(lambda r: httpx.Response(401)), sleep=lambda s: None)
    load.run(CARS, DEPTS, FakeDb(), make, workers=1, log=lines.append, every=1)
    assert lines and not any(user in l or pw in l for l in lines)


def test_service_departments_filter():
    depts = [*DEPTS, {"business": "A", "department": "Parts", "department_uuid": "p", "scopes": [MPI_SCOPE]},
             {"business": "C", "department": "Service", "department_uuid": "x", "scopes": ["other"]}]
    assert [d["department_uuid"] for d in load.service_departments(depts)] == ["d-a", "d-b"]


def _ddl_columns(table: str) -> list[str]:
    sql = (config.ROOT / "sql" / "src_mykaarma.sql").read_text()
    body = re.search(rf"create table dbo\.{table} \((.*?)\n\);", sql, re.S).group(1)
    body = re.sub(r"--[^\n]*", "", body)
    body = re.sub(r"\([^)]*\)", "", body)   # type sizes like decimal(12,2)
    return [p.split()[0] for p in body.replace("\n", " ").split(",") if p.strip() and not p.strip().startswith("primary key")]


@pytest.mark.parametrize("table,cols", [
    ("src_mykaarma__vin_lookups", extract.LOOKUP_COLS), ("src_mykaarma__inspections", extract.INSPECTION_COLS),
    ("src_mykaarma__recommendations", extract.RECOMMENDATION_COLS), ("src_mykaarma__inspection_associates", extract.ASSOCIATE_COLS)])
def test_ddl_matches_inserted_columns(table, cols):
    assert _ddl_columns(table) == list(cols)


def test_extracted_rows_have_exactly_the_table_columns():
    insp, recs, assoc = extract.rows(VIN, "A", "d", SUMMARY, DETAIL, NOW)
    assert tuple(insp) == extract.INSPECTION_COLS
    assert all(tuple(r) == extract.RECOMMENDATION_COLS for r in recs)
    assert all(tuple(a) == extract.ASSOCIATE_COLS for a in assoc)
    lookup = load.check_vin(api()(), CARS[0], DEPTS).lookup
    assert tuple(lookup) == extract.LOOKUP_COLS
