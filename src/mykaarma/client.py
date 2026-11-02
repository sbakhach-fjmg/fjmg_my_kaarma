"""myKaarma API over Basic auth.

Credentials go straight into httpx.BasicAuth and are never stored, formatted or logged here.
Errors carry the method, path and status only: never the request, its headers or the response body.
"""
from __future__ import annotations
import httpx

MPI_SCOPE = "multipoint.inspection.fetch"


class ConfigError(Exception):
    pass


class ApiError(Exception):
    pass


def _scope_name(s) -> str:
    return s if isinstance(s, str) else str(s.get("scope") or s.get("name") or "")


def _money(v):
    """Amounts come back as plain numbers or as {amount, unit} objects."""
    return v.get("amount") if isinstance(v, dict) else v


class MyKaarma:
    def __init__(self, username: str, password: str, base_url: str = "https://api.mykaarma.com",
                 timeout: float = 30, transport: httpx.BaseTransport | None = None):
        if not username or not password:
            raise ConfigError("MYKAARMA_USERNAME/MYKAARMA_PASSWORD not set in .env")
        self.base_url = base_url
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
        try:
            resp = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise ApiError(f"{method} {path} failed: {type(e).__name__}") from None
        if resp.status_code in (401, 403):
            raise ApiError(f"myKaarma rejected the credentials ({resp.status_code}) for {path}; "
                           "check MYKAARMA_USERNAME/MYKAARMA_PASSWORD and the scopes granted")
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

    def recommended_work(self, vin: str, department_uuid: str) -> list[dict]:
        """Recommendations on every multipoint inspection for this VIN in one department.

        Docs: our-products/inspection/service-cart/how-to-filter-mpis (needs the multipoint.inspection.fetch scope).
        """
        data = self._request("POST", f"/scprocessor/mpi/v1/dealerDepartment/{department_uuid}/multiPointInspections",
                             json={"vin": vin})
        rows = []
        for order_uuid, mpis in (data.get("mpisForOrdersMap") or {}).items():
            for mpi in mpis or []:
                mpi = mpi.get("multiPointInsepction") or mpi   # myKaarma's spelling
                for section in mpi.get("mpiSections") or []:
                    for check in section.get("checks") or []:
                        for rec in check.get("recommendations") or []:
                            rows.append({
                                "order_uuid": order_uuid,
                                "order_number": mpi.get("orderNumber", ""),
                                "section": section.get("name", ""),
                                "check": check.get("name", ""),
                                "description": rec.get("description") or check.get("description", ""),
                                "approval_state": rec.get("approvalState", ""),
                                "status": rec.get("status", ""),
                                "work_status": rec.get("workStatus", ""),
                                "amount": _money(rec.get("amount")),
                                "op_codes": rec.get("opCodes") or [],
                                "created_on": rec.get("createdOn", ""),
                            })
        return rows
