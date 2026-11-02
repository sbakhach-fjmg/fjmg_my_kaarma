"""mykaarma check [--save] | mykaarma recommendations VIN [--dept NAME_OR_UUID]"""
from __future__ import annotations
import argparse
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
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, ApiError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
