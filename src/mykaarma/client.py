"""myKaarma API over Basic auth.

Credentials go straight into httpx.BasicAuth and are never stored, formatted or logged here.
Errors carry the method, path and status only: never the request, its headers or the response body.
"""
from __future__ import annotations
import time
from datetime import datetime

import httpx

from mykaarma import extract

MPI_SCOPE = "multipoint.inspection.fetch"


class ConfigError(Exception):
    pass


class ApiError(Exception):
    pass


def _scope_name(s) -> str:
    return s if isinstance(s, str) else str(s.get("scope") or s.get("name") or "")


class MyKaarma:
    def __init__(self, username: str, password: str, base_url: str = "https://api.mykaarma.com",
                 timeout: float = 30, transport: httpx.BaseTransport | None = None, retries: int = 3,
                 sleep=time.sleep):
        if not username or not password:
            raise ConfigError("MYKAARMA_USERNAME/MYKAARMA_PASSWORD not set in .env")
        self.base_url = base_url
        self._retries, self._sleep = retries, sleep
        self._http = httpx.Client(base_url=base_url, auth=httpx.BasicAuth(username, password), timeout=timeout,
                                  transport=transport, headers={"accept": "application/json"})

    def __repr__(self) -> str:
        return f"MyKaarma(base_url={self.base_url!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method: str, path: str, **kw) -> dict:
        """Retries 429, 5xx and transport errors with backoff (1s, 2s, 4s...); 401/403 and other 4xx fail at once."""
        for attempt in range(self._retries):
            last = attempt == self._retries - 1
            try:
                resp = self._http.request(method, path, **kw)
            except httpx.HTTPError as e:
                if last:
                    raise ApiError(f"{method} {path} failed: {type(e).__name__}") from None
                self._sleep(2 ** attempt)
                continue
            if resp.status_code in (401, 403):
                raise ApiError(f"myKaarma rejected the credentials ({resp.status_code}) for {path}; "
                               "check MYKAARMA_USERNAME/MYKAARMA_PASSWORD and the scopes granted")
            if resp.status_code == 429 or resp.status_code >= 500:
                if last:
                    raise ApiError(f"{method} {path} returned {resp.status_code}")
                self._sleep(2 ** attempt)
                continue
            if resp.status_code >= 400:
                raise ApiError(f"{method} {path} returned {resp.status_code}")
            return resp.json()

    def check_access(self) -> list[dict]:
        """One row per department the credentials can reach, with that department's enabled scopes."""
        data = self._request("GET", "/developer/application/configurations")
        rows = []
        for biz in data.get("enabledBusinesses") or []:
            for dept in biz.get("departments") or []:
                rows.append({
                    "business": biz.get("name", ""),
                    "business_uuid": biz.get("uuid", ""),
                    "department": dept.get("name", ""),
                    "department_uuid": dept.get("uuid", ""),
                    "scopes": sorted(_scope_name(s) for s in dept.get("enabledDepartmentLevelScopes") or []),
                })
        return rows

    def inspections(self, vin: str, department_uuid: str) -> list[dict]:
        """Multipoint-inspection summaries for this VIN in one department (no line items; mpiSections is null).

        Docs: our-products/inspection/service-cart/how-to-filter-mpis (needs the multipoint.inspection.fetch scope).
        """
        data = self._request("POST", f"/scprocessor/mpi/v1/dealerDepartment/{department_uuid}/multiPointInspections",
                             json={"vin": vin})
        return [mpi for mpis in (data.get("mpisForOrdersMap") or {}).values() for mpi in mpis or []]

    def inspection_detail(self, department_uuid: str, order_uuid: str, mpi_uuid: str) -> dict:
        """Docs: our-products/inspection/service-cart/how-to-get-multipoint-inspection."""
        data = self._request("GET", f"/scprocessor/mpi/v1/dealerDepartment/{department_uuid}/order/{order_uuid}"
                                    f"/multiPointInspection/{mpi_uuid}")
        return data.get("multiPointInsepction") or {}   # myKaarma's spelling

    def vin_inspections(self, vin: str, department_uuid: str) -> list[tuple[dict, dict]]:
        """(summary, detail) for every inspection on this VIN in one department: one search, then one call each."""
        out = []
        for summary in self.inspections(vin, department_uuid):
            order = summary.get("orderIdentifiers") or {}
            out.append((summary, self.inspection_detail(department_uuid, order.get("orderUUID", ""), summary.get("uuid", ""))))
        return out

    def recommended_work(self, vin: str, department_uuid: str) -> list[dict]:
        """One row per real recommendation on every inspection for this VIN in one department (for the CLI)."""
        rows = []
        for summary, detail in self.vin_inspections(vin, department_uuid):
            insp, recs, _ = extract.rows(vin, "", department_uuid, summary, detail, datetime.now())
            for r in recs:
                rows.append({
                    "order_uuid": insp["order_uuid"] or "", "order_number": insp["order_number"] or "",
                    "order_date": str(insp["order_date"] or ""), "order_status": insp["order_status"] or "",
                    "recommended_total": insp["mk_recommended_total"], "approved_total": insp["mk_approved_total"],
                    "section": r["section"] or "", "check": r["check_name"] or "", "description": r["description"] or "",
                    "severity": r["severity"], "status": r["status"] or "", "approval_state": r["approval_state"] or "",
                    "amount": r["amount"], "op_code": r["op_code"] or "",
                })
        return rows
