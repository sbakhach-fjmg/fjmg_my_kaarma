"""mykaarma check [--save] | recommendations VIN [--dept NAME_OR_UUID] | load [--limit N] [--workers N] [--skip-fresh H]"""
from __future__ import annotations
import argparse
import os
import re
import sys

from mykaarma import config
from mykaarma.client import MPI_SCOPE, ApiError, ConfigError, MyKaarma

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")


def _client() -> MyKaarma:
    user, pw = config.credentials()
    return MyKaarma(user, pw, base_url=config.base_url())


def cmd_check(args) -> int:
    user, pw = config.credentials()
    print(f"credentials: {'set' if user and pw else 'missing'}")
    with _client() as mk:
        rows = mk.check_access()
    print(f"{len(rows)} department(s)")
    for r in rows:
        print(f"\n{r['business']} / {r['department']}\n  uuid:   {r['department_uuid']}\n  scopes: {', '.join(r['scopes']) or '(none)'}")
    if args.save:
        config.save_departments([{k: r[k] for k in ("business", "department", "department_uuid", "scopes")} for r in rows])
        print(f"\nsaved to {config.DEPARTMENTS_FILE}")
    return 0


def _usd(v) -> str:
    return f"${v:,.2f}" if isinstance(v, (int, float)) else "-"


def _pick(depts: list[dict], want: str | None, all_depts: bool = False) -> list[dict]:
    """Default: each store's Service department, where inspections live. --dept narrows, --all widens."""
    if not want:
        return depts if all_depts else [d for d in depts if d["department"].strip().lower() == "service"]
    w = want.lower()
    return [d for d in depts if d["department_uuid"].lower() == w or w in f"{d['business']} {d['department']}".lower()]


def fetch_all(mk: MyKaarma, vin: str, depts: list[dict]) -> list[tuple[dict, list[dict] | None, str]]:
    """(department, recommendations, note) per department. A failing department doesn't stop the others."""
    out = []
    for d in depts:
        if d.get("scopes") and MPI_SCOPE not in d["scopes"]:
            out.append((d, None, f"skipped: no {MPI_SCOPE} scope"))
            continue
        try:
            out.append((d, mk.recommended_work(vin, d["department_uuid"]), ""))
        except ApiError as e:
            out.append((d, None, f"error: {e}"))
    return out


def cmd_recommendations(args) -> int:
    vin = args.vin.strip().upper()
    if not VIN_RE.match(vin):
        print(f"not a valid 17-character VIN: {args.vin!r}", file=sys.stderr)
        return 2
    depts = _pick(config.departments(), args.dept, args.all)
    if not depts:
        print("no departments: run `mykaarma check --save` first" if not args.dept else f"no department matches {args.dept!r}",
              file=sys.stderr)
        return 2
    with _client() as mk:
        results = fetch_all(mk, vin, depts)
    total = 0
    for d, recs, note in results:
        print(f"\n{d['business']} / {d['department']}")
        if recs is None:
            print(f"  {note}")
            continue
        if not recs:
            print("  no recommendations")
        order = None
        for r in recs:
            total += 1
            if r["order_uuid"] != order:
                order = r["order_uuid"]
                print(f"  RO {r['order_number'] or order}  {r['order_date']}  ({r['order_status']})"
                      f"  recommended {_usd(r['recommended_total'])}, approved {_usd(r['approved_total'])}")
            label = r["check"] + (f": {r['description']}" if r["description"] else "")
            print(f"    - [{r['severity'] or '-':<6}] {label}  {_usd(r['amount'])}"
                  f"  {r['status']}{'/' + r['approval_state'] if r['approval_state'] not in ('', r['status']) else ''}"
                  f"{'  op ' + r['op_code'] if r['op_code'] else ''}")
    print(f"\n{total} recommendation(s) for {vin}")
    return 0


def _vin_file(path: str) -> list[dict]:
    """[{vin, stock_no, dealer_code, in_stock: False}] from a CSV with a header row: vin, dealer_code, optional stock_no.
    For VINs outside current stock (e.g. a fj-internal-allocation snapshot): each is checked at the store given."""
    import csv
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if rows and not {"vin", "dealer_code"} <= set(rows[0]):
        raise ConfigError(f"{path} needs a header row with vin and dealer_code columns")
    return [{"vin": (r["vin"] or "").strip().upper(), "stock_no": (r.get("stock_no") or "").strip() or None,
             "dealer_code": (r["dealer_code"] or "").strip().upper(), "in_stock": False} for r in rows]


def cmd_load(args) -> int:
    from mykaarma import load
    from mykaarma.db import Mssql
    conn = os.environ.get("MSSQL_CONNECTION", "")
    if not conn:
        raise ConfigError("MSSQL_CONNECTION not set in .env")
    stores = load.store_departments(config.stores())
    _client().close()   # fail on missing credentials before touching the database
    db = Mssql(conn)
    try:
        db.ensure_tables()
        cars = _vin_file(args.vins) if args.vins else db.inventory_vins()
        bad = [c for c in cars if not VIN_RE.match(c["vin"] or "")]
        cars = [c for c in cars if c not in bad]
        if bad:
            print(f"ignoring {len(bad)} inventory row(s) with an invalid VIN")
        full = args.limit is None and not args.vins
        if not full:
            cars = cars[:args.limit]
        unmapped = sorted({c["dealer_code"] for c in cars} - set(stores))
        if unmapped:
            print(f"no stores.json entry for {', '.join(map(str, unmapped))}: those VINs are recorded with an error")
        stats = load.run(cars, stores, db, _client, workers=args.workers, skip_fresh_hours=args.skip_fresh, track_inventory=full)
    finally:
        db.close()
    print(f"done: {stats['checked']} checked, {stats['skipped']} skipped, {stats['errors']} with errors, "
          f"{stats['inspections']} inspections, {stats['recommendations']} recommendations")
    return 1 if stats["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    config.load_env()
    p = argparse.ArgumentParser(prog="mykaarma")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="list the departments and scopes these credentials can reach")
    c.add_argument("--save", action="store_true", help="write them to departments.json")
    c.set_defaults(func=cmd_check)
    r = sub.add_parser("recommendations", help="recommended work for a VIN across saved departments")
    r.add_argument("vin")
    r.add_argument("--dept", help="limit to matching departments (UUID or part of 'store department')")
    r.add_argument("--all", action="store_true", help="query every saved department, not just Service")
    r.set_defaults(func=cmd_recommendations)
    ld = sub.add_parser("load", help="check each used-inventory VIN at its current store and write results to SQL Server")
    ld.add_argument("--limit", type=int, help="only the first N VINs (doesn't update which VINs are in inventory)")
    ld.add_argument("--vins", metavar="CSV", help="check these VINs (columns vin, dealer_code, optional stock_no) instead "
                    "of used inventory; they're never marked as in inventory")
    ld.add_argument("--workers", type=int, default=6, help="parallel API workers (default 6)")
    ld.add_argument("--skip-fresh", type=float, default=20, metavar="HOURS",
                    help="skip VINs checked without error in the last HOURS (default 20; 0 = recheck all)")
    ld.set_defaults(func=cmd_load)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ApiError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
