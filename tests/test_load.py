import json
import re
from datetime import datetime, timedelta

import httpx
import pytest

from mykaarma import config, extract, load
from mykaarma.client import ApiError, MyKaarma

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


STORES = {"NBMBN": {"business": "A", "department_uuid": "d-a"}, "BHMBN": {"business": "B", "department_uuid": "d-b"}}
CARS = [{"vin": VIN, "stock_no": "S1", "dealer_code": "NBMBN"}, {"vin": "1HGCM82633A004352", "stock_no": "S2", "dealer_code": "BHMBN"}]


class FakeDb:
    """Same contract as db.Mssql: errors keep the old rows, success replaces them."""
    def __init__(self):
        self.lookups, self.rows, self.calls = {}, {}, []

    def fresh_vins(self, hours, now):
        return {v for v, l in self.lookups.items() if l["error"] is None and l["checked_at"] >= now - timedelta(hours=hours)}

    def replace_vin(self, lookup, inspections, recs, associates, in_stock=True):
        self.calls.append(lookup["vin"])
        seen = {"in_inventory": True, "first_seen": lookup["checked_at"], "last_seen": lookup["checked_at"]}
        old = self.lookups.get(lookup["vin"], seen)
        if lookup["error"]:
            self.lookups[lookup["vin"]] = {**old, **{k: v for k, v in lookup.items() if k != "inspections_found"}}
        else:
            self.lookups[lookup["vin"]] = {**old, **lookup}
            self.rows[lookup["vin"]] = (inspections, recs, associates)
        if in_stock:
            self.lookups[lookup["vin"]]["in_inventory"] = True
        elif old is seen:
            self.lookups[lookup["vin"]]["in_inventory"] = False

    def mark_inventory(self, in_stock, now):
        gone = 0
        for v, l in self.lookups.items():
            if v in in_stock:
                l.update(in_inventory=True, last_seen=now)
            elif l["in_inventory"]:
                l["in_inventory"] = False
                gone += 1
        return gone


def test_load_writes_each_vin_once_and_vins_without_inspections_still_get_a_lookup():
    db = FakeDb()
    stats = load.run(CARS, STORES, db, api(), workers=2, log=lambda s: None)
    assert sorted(db.calls) == sorted(c["vin"] for c in CARS)
    assert stats["checked"] == 2 and stats["errors"] == 0 and stats["inspections"] == 1 and stats["recommendations"] == 3
    assert db.lookups[VIN]["inspections_found"] == 1 and db.lookups[VIN]["departments_checked"] == 1
    assert db.lookups["1HGCM82633A004352"]["inspections_found"] == 0
    assert db.rows[VIN][0][0]["store"] == "A"


def test_only_the_current_store_is_checked():
    seen = []

    def make():
        def handler(req):
            seen.append(req.url.path.split("/")[5])
            return api()()._http._transport.handle_request(req)
        return MyKaarma("u", "p", transport=httpx.MockTransport(handler), sleep=lambda s: None)

    load.run(CARS, STORES, FakeDb(), make, workers=1, log=lambda s: None)
    assert seen.count("d-b") == 1                      # BHMBN car: one search, nothing found
    assert set(seen) == {"d-a", "d-b"} and seen.count("d-a") == 2   # NBMBN car: search + one detail


def test_unmapped_store_is_recorded_as_an_error_without_api_calls():
    db = FakeDb()
    car = {"vin": "1FTFW1ET5DFC10312", "stock_no": "S9", "dealer_code": "XXXXX"}
    make = lambda: MyKaarma("u", "p", transport=httpx.MockTransport(lambda r: pytest.fail("API called")))
    stats = load.run([car], STORES, db, make, workers=1, log=lambda s: None)
    assert stats["errors"] == 1 and "no myKaarma department for store 'XXXXX'" in db.lookups[car["vin"]]["error"]
    assert db.lookups[car["vin"]]["departments_checked"] == 0


def test_failing_department_keeps_previous_rows_and_records_error():
    db = FakeDb()
    load.run(CARS[:1], STORES, db, api(), workers=1, log=lambda s: None)
    before = db.rows[VIN]
    stats = load.run(CARS[:1], STORES, db, api(fail_dept="d-a"), workers=1, skip_fresh_hours=0, log=lambda s: None)
    assert stats["errors"] == 1
    assert db.rows[VIN] is before
    assert "1 department(s) failed" in db.lookups[VIN]["error"] and "500" in db.lookups[VIN]["error"]
    assert db.lookups[VIN]["inspections_found"] == 1   # from the last good check


def test_skip_fresh_and_errored_vins_are_retried():
    db = FakeDb()
    load.run(CARS, STORES, db, api(fail_dept="d-b"), workers=1, log=lambda s: None)
    db.calls.clear()
    load.run(CARS, STORES, db, api(), workers=1, log=lambda s: None)
    assert db.calls == ["1HGCM82633A004352"]                        # only the errored VIN is retried
    db.calls.clear()
    stats = load.run(CARS, STORES, db, api(), workers=1, log=lambda s: None)
    assert db.calls == [] and stats["skipped"] == 2


def test_vins_that_leave_inventory_are_kept_and_flagged_not_rechecked():
    db = FakeDb()
    sold = CARS[1]["vin"]
    load.run(CARS, STORES, db, api(), workers=1, track_inventory=True, log=lambda s: None)
    first_seen = db.lookups[VIN]["first_seen"]
    load.run(CARS[:1], STORES, db, api(), workers=1, skip_fresh_hours=0, log=lambda s: None)
    assert db.lookups[sold]["in_inventory"]                        # a --limit run doesn't judge inventory
    db.calls.clear()
    stats = load.run(CARS[:1], STORES, db, api(), workers=1, skip_fresh_hours=0, track_inventory=True, log=lambda s: None)
    assert stats["left_inventory"] == 1 and db.calls == [VIN]     # the sold VIN isn't rechecked
    assert set(db.lookups) == {VIN, sold} and sold in db.rows      # ... and nothing is deleted
    assert not db.lookups[sold]["in_inventory"] and db.lookups[VIN]["in_inventory"]
    assert db.lookups[VIN]["first_seen"] == first_seen
    load.run(CARS, STORES, db, api(), workers=1, track_inventory=True, log=lambda s: None)
    assert db.lookups[sold]["in_inventory"]                        # back in stock


def test_failed_write_fails_the_vin_not_the_run():
    class TooLong(FakeDb):
        def replace_vin(self, lookup, inspections, recs, associates, in_stock=True):
            if recs:
                raise ValueError("String or binary data would be truncated")
            super().replace_vin(lookup, inspections, recs, associates)

    db = TooLong()
    stats = load.run(CARS, STORES, db, api(), workers=2, log=lambda s: None)
    assert stats["checked"] == 2 and stats["errors"] == 1 and stats["recommendations"] == 0
    assert db.lookups[VIN]["error"] == "database write failed: ValueError: String or binary data would be truncated"
    assert db.lookups["1HGCM82633A004352"]["error"] is None


def test_unexpected_payload_fails_the_vin_not_the_run(monkeypatch):
    db = FakeDb()
    monkeypatch.setattr(extract, "rows", lambda *a: (_ for _ in ()).throw(KeyError("x")))
    stats = load.run(CARS, STORES, db, api(), workers=2, log=lambda s: None)
    assert stats["checked"] == 2 and stats["errors"] == 1
    assert db.lookups[VIN]["error"] == "unexpected KeyError while extracting"


def test_progress_output_has_no_secrets():
    lines = []
    user, pw = "user-x", "pw-y-secret"
    make = lambda: MyKaarma(user, pw, transport=httpx.MockTransport(lambda r: httpx.Response(401)), sleep=lambda s: None)
    load.run(CARS, STORES, FakeDb(), make, workers=1, log=lines.append, every=1)
    assert lines and not any(user in l or pw in l for l in lines)


def test_stores_json_maps_every_store_to_one_department():
    stores = config.stores()
    assert len(stores) == 12 and "L2MBN" in stores
    assert all(len(v["department_uuid"]) == 64 for v in stores.values())
    assert len({v["department_uuid"] for v in stores.values()}) == 12
    assert set(load.store_departments(stores)["NBMBN"]) == {"business", "department_uuid"}


def _ddl_columns(table: str) -> list[str]:
    sql = (config.ROOT / "sql" / "src_mykaarma.sql").read_text()
    body = re.search(rf"create table dbo\.{table} \((.*?)\n\);", sql, re.S).group(1)
    body = re.sub(r"--[^\n]*", "", body)
    body = re.sub(r"\([^)]*\)", "", body)   # type sizes like decimal(12,2)
    return [p.split()[0] for p in body.replace("\n", " ").split(",") if p.strip() and not p.strip().startswith("primary key")]


@pytest.mark.parametrize("table,cols", [
    ("src_mykaarma__vin_lookups", extract.LOOKUP_COLS + extract.INVENTORY_COLS),
    ("src_mykaarma__inspections", extract.INSPECTION_COLS),
    ("src_mykaarma__recommendations", extract.RECOMMENDATION_COLS), ("src_mykaarma__inspection_associates", extract.ASSOCIATE_COLS)])
def test_ddl_matches_inserted_columns(table, cols):
    assert _ddl_columns(table) == list(cols)


def test_extracted_rows_have_exactly_the_table_columns():
    insp, recs, assoc = extract.rows(VIN, "A", "d", SUMMARY, DETAIL, NOW)
    assert tuple(insp) == extract.INSPECTION_COLS
    assert all(tuple(r) == extract.RECOMMENDATION_COLS for r in recs)
    assert all(tuple(a) == extract.ASSOCIATE_COLS for a in assoc)
    lookup = load.check_vin(api()(), CARS[0], [STORES["NBMBN"]]).lookup
    assert tuple(lookup) == extract.LOOKUP_COLS


def test_vins_outside_stock_are_loaded_without_being_marked_in_inventory():
    db = FakeDb()
    cars = [{**c, "in_stock": False} for c in CARS]
    load.run(cars, STORES, db, api(), workers=1, log=lambda s: None)
    assert all(not db.lookups[c["vin"]]["in_inventory"] for c in CARS)
    assert set(db.rows) == {c["vin"] for c in CARS}
