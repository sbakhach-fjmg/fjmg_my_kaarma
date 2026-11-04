"""Check every used-inventory VIN at its current store's Service department and write the results to SQL Server.

API calls run in a thread pool (one MyKaarma client per thread); all database writes happen on the calling
thread, one transaction per VIN, so a long run can be stopped and resumed with --skip-fresh.
"""
from __future__ import annotations
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Protocol

from mykaarma import extract
from mykaarma.client import ApiError, MyKaarma


class Sink(Protocol):
    def fresh_vins(self, hours: float, now: datetime) -> set[str]: ...
    def replace_vin(self, lookup: dict, inspections: list[dict], recs: list[dict], associates: list[dict],
                    in_stock: bool = True) -> None: ...
    def mark_inventory(self, in_stock: list[str], now: datetime) -> int: ...


@dataclass
class VinResult:
    lookup: dict
    inspections: list[dict] = field(default_factory=list)
    recs: list[dict] = field(default_factory=list)
    associates: list[dict] = field(default_factory=list)
    in_stock: bool = True   # False for VINs from load --vins: never flagged as in inventory


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def check_vin(mk: MyKaarma, car: dict, depts: list[dict]) -> VinResult:
    """All departments for one VIN. Any department failing marks the whole VIN as errored."""
    now = utcnow()
    res = VinResult({"vin": car["vin"], "stock_no": car.get("stock_no"), "dealer_code": car.get("dealer_code"),
                     "checked_at": now, "departments_checked": 0, "departments_failed": 0,
                     "inspections_found": 0, "error": None}, in_stock=car.get("in_stock", True))
    errors = []
    for d in depts:
        res.lookup["departments_checked"] += 1
        try:
            pairs = mk.vin_inspections(car["vin"], d["department_uuid"])
        except ApiError as e:
            res.lookup["departments_failed"] += 1
            errors.append(f"{d['business']}: {e}")
            continue
        for summary, detail in pairs:
            insp, recs, assoc = extract.rows(car["vin"], d["business"], d["department_uuid"], summary, detail, now)
            res.inspections.append(insp)
            res.recs.extend(recs)
            res.associates.extend(assoc)
    res.lookup["inspections_found"] = len(res.inspections)
    if errors:
        res.lookup["error"] = f"{len(errors)} department(s) failed; first: {errors[0]}"[:400]
    return res


def store_departments(stores: dict[str, dict]) -> dict[str, dict]:
    """stores.json entries in the shape check_vin takes: {dealer_code: {business, department_uuid}}."""
    return {code: {"business": v["mk_store"], "department_uuid": v["department_uuid"]} for code, v in stores.items()}


def _failed(car: dict, departments_checked: int, error: str) -> VinResult:
    return VinResult({"vin": car["vin"], "stock_no": car.get("stock_no"), "dealer_code": car.get("dealer_code"),
                      "checked_at": utcnow(), "departments_checked": departments_checked, "departments_failed": 0,
                      "inspections_found": 0, "error": error}, in_stock=car.get("in_stock", True))


def run(cars: list[dict], stores: dict[str, dict], sink: Sink, make_client: Callable[[], MyKaarma], *,
        workers: int = 6, skip_fresh_hours: float = 20, track_inventory: bool = False,
        log: Callable[[str], None] = print, every: int = 100) -> dict:
    """Each VIN is checked only at its current store's Service department (stores: dealer_code -> department).
    track_inventory (full runs only): `cars` is all of used inventory, so VINs not in it are flagged as gone.
    Returns counts: checked, skipped, errors, inspections, recommendations, left_inventory."""
    all_vins = [c["vin"] for c in cars]
    fresh = sink.fresh_vins(skip_fresh_hours, utcnow()) if skip_fresh_hours > 0 else set()
    todo = [c for c in cars if c["vin"] not in fresh]
    stats = {"checked": 0, "skipped": len(cars) - len(todo), "errors": 0, "inspections": 0, "recommendations": 0,
             "left_inventory": 0}
    log(f"{len(cars)} VINs, {stats['skipped']} checked recently (skipped), {len(todo)} to check "
        f"with {workers} workers")

    local = threading.local()
    clients: list[MyKaarma] = []
    lock = threading.Lock()

    def work(car):
        dept = stores.get((car.get("dealer_code") or "").upper())
        if dept is None:
            return _failed(car, 0, f"no myKaarma department for store {car.get('dealer_code')!r} in stores.json")
        if not hasattr(local, "mk"):
            local.mk = make_client()
            with lock:
                clients.append(local.mk)
        try:
            return check_vin(local.mk, car, [dept])
        except Exception as e:   # an unexpected payload shape fails this VIN, not the run
            return _failed(car, 1, f"unexpected {type(e).__name__} while extracting")

    start = time.monotonic()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for fut in as_completed([pool.submit(work, c) for c in todo]):
                res = fut.result()
                try:
                    sink.replace_vin(res.lookup, res.inspections, res.recs, res.associates, in_stock=res.in_stock)
                except Exception as e:   # a row the tables can't hold fails this VIN; a dead connection still stops the run
                    res = _failed(res.lookup, res.lookup["departments_checked"],
                                  f"database write failed: {type(e).__name__}: {e}"[:400])
                    sink.replace_vin(res.lookup, [], [], [], in_stock=res.in_stock)
                stats["checked"] += 1
                stats["errors"] += bool(res.lookup["error"])
                stats["inspections"] += len(res.inspections)
                stats["recommendations"] += len(res.recs)
                if stats["checked"] % every == 0 or stats["checked"] == len(todo):
                    rate = stats["checked"] / max(time.monotonic() - start, 1e-9) * 60
                    log(f"  {stats['checked']}/{len(todo)} VINs  errors={stats['errors']}  "
                        f"inspections={stats['inspections']}  recs={stats['recommendations']}  {rate:.0f} VIN/min")
    finally:
        for c in clients:
            c.close()
    if track_inventory:
        stats["left_inventory"] = sink.mark_inventory(all_vins, utcnow())
        log(f"{stats['left_inventory']} VIN(s) left used inventory since the last full run (kept, in_inventory = 0)")
    return stats
