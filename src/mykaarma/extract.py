"""Turn an inspection summary + detail into rows for the src_mykaarma__* tables.

Customer identifiers are dropped everywhere, including from the stored raw payload; employee identifiers are kept.
"""
from __future__ import annotations
import json
from datetime import date, datetime, timezone

SEVERITY_RANK = {"red": 3, "yellow": 2, "green": 1}

INSPECTION_COLS = ("mpi_uuid", "vin", "store", "department_uuid", "order_uuid", "order_number", "order_date",
                   "order_status", "mpi_status", "mk_recommended_total", "mk_approved_total", "mk_rec_count",
                   "mk_approve_count", "mk_decline_count", "mpi_created_on", "mpi_updated_on", "loaded_at", "raw")
RECOMMENDATION_COLS = ("recommendation_uuid", "mpi_uuid", "vin", "section", "check_name", "description", "severity",
                       "severity_rank", "status", "approval_state", "decision", "amount", "op_code", "approver_name",
                       "last_updated_by", "rec_created_on", "rec_updated_on", "loaded_at")
ASSOCIATE_COLS = ("mpi_uuid", "role", "associate_uuid", "associate_name")
LOOKUP_COLS = ("vin", "stock_no", "dealer_code", "checked_at", "departments_checked", "departments_failed",
               "inspections_found", "error")


def severity(check: dict) -> str:
    """Worst colour among the check's inspection fields (red > yellow > green); '' if none."""
    states = [f.get("state") for f in check.get("fieldList") or [] if f.get("state") in SEVERITY_RANK]
    return max(states, key=SEVERITY_RANK.get) if states else ""


def money(v):
    """Amounts come back as plain numbers or as {amount, unit} objects."""
    return v.get("amount") if isinstance(v, dict) else v


def decision(rec: dict) -> str:
    """approved / declined / pending, from approvalState first, then status."""
    for v in (rec.get("approvalState"), rec.get("status")):
        v = (v or "").lower()
        if v.startswith("approv"):
            return "approved"
        if v.startswith("declin"):
            return "declined"
    return "pending"


def _ts(ms) -> datetime | None:
    """Epoch milliseconds -> naive UTC datetime, second precision (datetime2(0))."""
    if not isinstance(ms, (int, float)) or not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None, microsecond=0)


def _date(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def _name(person) -> str | None:
    return (person.get("dealerAssociateName") or None) if isinstance(person, dict) else (person or None)


def _real(check: dict) -> list[dict]:
    """Recommendation slots that hold a recommendation; empty template slots have no status."""
    return [r for r in check.get("recommendations") or [] if r.get("status")]


def rows(vin: str, store: str, department_uuid: str, summary: dict, detail: dict,
         loaded_at: datetime) -> tuple[dict, list[dict], list[dict]]:
    """(inspection row, recommendation rows, associate rows) for one inspection."""
    mpi = {**summary, **{k: v for k, v in (detail or {}).items() if v is not None}}   # detail wins; summary fills gaps
    order = mpi.get("orderIdentifiers") or {}
    counts = mpi.get("mpiCounts") or {}
    mpi_uuid = mpi.get("uuid")
    raw = {k: v for k, v in mpi.items() if k != "customerIdentifiers"}
    inspection = {
        "mpi_uuid": mpi_uuid, "vin": vin, "store": store, "department_uuid": department_uuid,
        "order_uuid": order.get("orderUUID"), "order_number": order.get("orderNumber"),
        "order_date": _date(order.get("orderDate")), "order_status": order.get("orderStatus"),
        "mpi_status": mpi.get("status"),
        "mk_recommended_total": mpi.get("recommendedTotal"), "mk_approved_total": mpi.get("approvedTotal"),
        "mk_rec_count": counts.get("totalRecommendationCount"), "mk_approve_count": counts.get("approveCount"),
        "mk_decline_count": counts.get("declineCount"),
        "mpi_created_on": _ts(mpi.get("createdOn")), "mpi_updated_on": _ts(mpi.get("updatedOn")),
        "loaded_at": loaded_at, "raw": json.dumps(raw, separators=(",", ":"), default=str),
    }
    recs = []
    for section in mpi.get("mpiSections") or []:
        for check in section.get("checks") or []:
            sev = severity(check)
            for rec in _real(check):
                recs.append({
                    "recommendation_uuid": rec.get("uuid"), "mpi_uuid": mpi_uuid, "vin": vin,
                    "section": section.get("name"), "check_name": check.get("name"),
                    "description": rec.get("description"), "severity": sev, "severity_rank": SEVERITY_RANK.get(sev, 0),
                    "status": rec.get("status"), "approval_state": rec.get("approvalState"), "decision": decision(rec),
                    "amount": money(rec.get("amount")),
                    "op_code": ", ".join(map(str, rec.get("opCodes") or [])) or (str(check["laborOpCode"]) if check.get("laborOpCode") else None),
                    "approver_name": rec.get("approverName"), "last_updated_by": _name(rec.get("lastUpdatedBy")),
                    "rec_created_on": _ts(rec.get("createdOn")), "rec_updated_on": _ts(rec.get("updatedOn")),
                    "loaded_at": loaded_at,
                })
    people = [("advisor", mpi.get("dealerAssociateIdentifiers"))]
    people += [("technician", p) for p in mpi.get("technicianIdentifiers") or []]
    people += [("assignee", p) for p in mpi.get("assignees") or []]
    associates, seen = [], set()
    for role, p in people:
        if isinstance(p, dict) and p.get("dealerAssociateUUID") and (role, p["dealerAssociateUUID"]) not in seen:
            seen.add((role, p["dealerAssociateUUID"]))
            associates.append({"mpi_uuid": mpi_uuid, "role": role, "associate_uuid": p["dealerAssociateUUID"],
                               "associate_name": _name(p)})
    return inspection, recs, associates
