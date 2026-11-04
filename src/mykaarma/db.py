"""SQL Server side: the used-inventory VIN list in, the src_mykaarma__* tables out.

Modelled on fj-internal-allocation batch/sources.py (mssql-python, Windows integrated auth via MSSQL_CONNECTION).
The tables come from sql/src_mykaarma.sql, run once by hand; this module never creates them.
"""
from __future__ import annotations
from datetime import datetime, timedelta

from mykaarma import config
from mykaarma.extract import ASSOCIATE_COLS, INSPECTION_COLS, INVENTORY_COLS, LOOKUP_COLS, RECOMMENDATION_COLS

SQL_DIR = config.ROOT / "sql"
T_LOOKUPS = "dbo.src_mykaarma__vin_lookups"
T_INSPECTIONS = "dbo.src_mykaarma__inspections"
T_RECS = "dbo.src_mykaarma__recommendations"
T_ASSOC = "dbo.src_mykaarma__inspection_associates"
V_SUMMARY = "dbo.vw_mykaarma__vin_recommendation_summary"
TABLES = {T_LOOKUPS: LOOKUP_COLS + INVENTORY_COLS, T_INSPECTIONS: INSPECTION_COLS, T_RECS: RECOMMENDATION_COLS, T_ASSOC: ASSOCIATE_COLS}


def _insert(table: str, cols: tuple[str, ...]) -> str:
    return f"insert into {table} ({', '.join(f'[{c}]' for c in cols)}) values ({', '.join('?' for _ in cols)})"


class Mssql:
    def __init__(self, connection_string: str):
        from mssql_python import connect
        self.con = connect(connection_string)

    def close(self) -> None:
        self.con.close()

    def _rows(self, sql: str, params=()) -> list[tuple]:
        cur = self.con.cursor()
        cur.execute(sql, params)
        return [tuple(r) for r in cur.fetchall()]

    def ensure_tables(self) -> None:
        missing = [name for name in [*TABLES, V_SUMMARY] if self._rows("select object_id(?)", (name,))[0][0] is None]
        missing += [f"{t}.{c}" for t, cols in TABLES.items() if t not in missing
                    for c in cols if self._rows("select col_length(?, ?)", (t, c))[0][0] is None]
        if missing:
            raise RuntimeError(f"missing {', '.join(missing)}: run sql/src_mykaarma.sql against this database")

    def inventory_vins(self) -> list[dict]:
        """[{vin, stock_no, dealer_code}] for every used VIN in stock."""
        return [{"vin": v, "stock_no": s, "dealer_code": d}
                for v, s, d in self._rows((SQL_DIR / "current_inventory_used.sql").read_text())]

    def fresh_vins(self, hours: float, now: datetime) -> set[str]:
        """VINs checked without error in the last `hours`; `now` is naive UTC like checked_at."""
        return {r[0] for r in self._rows(f"select vin from {T_LOOKUPS} where error is null and checked_at >= ?",
                                         (now - timedelta(hours=hours),))}

    def _upsert_lookup(self, cur, lookup: dict, cols: tuple[str, ...], in_stock: bool) -> None:
        """Update `cols` on the VIN's lookup row, or insert it seen now. first_seen is never overwritten. A VIN checked
        with in_stock False (load --vins) is never flagged as in inventory; a new row for it gets in_inventory = 0."""
        in_stock = int(in_stock)
        cur.execute(f"update {T_LOOKUPS} set {', '.join(f'[{c}] = ?' for c in cols)}, "
                    f"in_inventory = case when ? = 1 then 1 else in_inventory end where vin = ?",
                    [lookup[c] for c in cols] + [in_stock, lookup["vin"]])
        if cur.rowcount == 0:
            row = {**lookup, "in_inventory": in_stock, "first_seen": lookup["checked_at"], "last_seen": lookup["checked_at"]}
            cur.execute(_insert(T_LOOKUPS, LOOKUP_COLS + INVENTORY_COLS), [row[c] for c in LOOKUP_COLS + INVENTORY_COLS])

    def replace_vin(self, lookup: dict, inspections: list[dict], recs: list[dict], associates: list[dict],
                    in_stock: bool = True) -> None:
        """One transaction per VIN. Success replaces its inspection rows; an error only updates the lookup, so the
        previous inspections stay and the next run retries the VIN."""
        cur = self.con.cursor()
        vin = lookup["vin"]
        try:
            if lookup["error"]:
                self._upsert_lookup(cur, lookup, tuple(c for c in LOOKUP_COLS if c not in ("vin", "inspections_found")), in_stock)
            else:
                sub = f"select mpi_uuid from {T_INSPECTIONS} where vin = ?"
                cur.execute(f"delete from {T_ASSOC} where mpi_uuid in ({sub})", (vin,))
                cur.execute(f"delete from {T_RECS} where vin = ?", (vin,))
                cur.execute(f"delete from {T_INSPECTIONS} where vin = ?", (vin,))
                self._upsert_lookup(cur, lookup, tuple(c for c in LOOKUP_COLS if c != "vin"), in_stock)
                for table, cols, rows in ((T_INSPECTIONS, INSPECTION_COLS, inspections),
                                          (T_RECS, RECOMMENDATION_COLS, recs), (T_ASSOC, ASSOCIATE_COLS, associates)):
                    if rows:
                        cur.executemany(_insert(table, cols), [[r[c] for c in cols] for r in rows])
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def mark_inventory(self, in_stock: list[str], now: datetime) -> int:
        """Stamp last_seen on VINs in used inventory and flag the rest in_inventory = 0 (their rows are kept).
        Returns how many VINs left inventory since the last full run."""
        cur = self.con.cursor()
        try:
            cur.execute("create table #stock (vin char(17) primary key)")
            for i in range(0, len(in_stock), 500):
                cur.executemany("insert into #stock (vin) values (?)", [[v] for v in in_stock[i:i + 500]])
            cur.execute(f"update {T_LOOKUPS} set in_inventory = 1, last_seen = ? where vin in (select vin from #stock)", (now,))
            cur.execute(f"update {T_LOOKUPS} set in_inventory = 0 where in_inventory = 1 and vin not in (select vin from #stock)")
            n = cur.rowcount
            cur.execute("drop table #stock")
            self.con.commit()
            return n
        except Exception:
            self.con.rollback()
            raise
