"""SQL Server side: the used-inventory VIN list in, the src_mykaarma__* tables out.

Modelled on fj-internal-allocation batch/sources.py (mssql-python, Windows integrated auth via MSSQL_CONNECTION).
The tables come from sql/src_mykaarma.sql, run once by hand; this module never creates them.
"""
from __future__ import annotations
from datetime import datetime, timedelta

from mykaarma import config
from mykaarma.extract import ASSOCIATE_COLS, INSPECTION_COLS, LOOKUP_COLS, RECOMMENDATION_COLS

SQL_DIR = config.ROOT / "sql"
T_LOOKUPS = "dbo.src_mykaarma__vin_lookups"
T_INSPECTIONS = "dbo.src_mykaarma__inspections"
T_RECS = "dbo.src_mykaarma__recommendations"
T_ASSOC = "dbo.src_mykaarma__inspection_associates"
V_SUMMARY = "dbo.vw_mykaarma__vin_recommendation_summary"
TABLES = {T_LOOKUPS: LOOKUP_COLS, T_INSPECTIONS: INSPECTION_COLS, T_RECS: RECOMMENDATION_COLS, T_ASSOC: ASSOCIATE_COLS}


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
        if missing:
            raise RuntimeError(f"missing {', '.join(missing)}: run sql/src_mykaarma.sql once against this database")

    def inventory_vins(self) -> list[dict]:
        """[{vin, stock_no, dealer_code}] for every used VIN in stock."""
        return [{"vin": v, "stock_no": s, "dealer_code": d}
                for v, s, d in self._rows((SQL_DIR / "current_inventory_used.sql").read_text())]

    def fresh_vins(self, hours: float, now: datetime) -> set[str]:
        """VINs checked without error in the last `hours`; `now` is naive UTC like checked_at."""
        return {r[0] for r in self._rows(f"select vin from {T_LOOKUPS} where error is null and checked_at >= ?",
                                         (now - timedelta(hours=hours),))}

    def replace_vin(self, lookup: dict, inspections: list[dict], recs: list[dict], associates: list[dict]) -> None:
        """One transaction per VIN. Success replaces all its rows; an error only updates the lookup, so the
        previous inspections stay and the next run retries the VIN."""
        cur = self.con.cursor()
        vin = lookup["vin"]
        try:
            if lookup["error"]:
                cur.execute(f"update {T_LOOKUPS} set checked_at = ?, departments_checked = ?, departments_failed = ?, "
                            f"error = ?, stock_no = ?, dealer_code = ? where vin = ?",
                            (lookup["checked_at"], lookup["departments_checked"], lookup["departments_failed"],
                             lookup["error"], lookup["stock_no"], lookup["dealer_code"], vin))
                if cur.rowcount == 0:
                    cur.execute(_insert(T_LOOKUPS, LOOKUP_COLS), [lookup[c] for c in LOOKUP_COLS])
            else:
                sub = f"select mpi_uuid from {T_INSPECTIONS} where vin = ?"
                cur.execute(f"delete from {T_ASSOC} where mpi_uuid in ({sub})", (vin,))
                cur.execute(f"delete from {T_RECS} where vin = ?", (vin,))
                cur.execute(f"delete from {T_INSPECTIONS} where vin = ?", (vin,))
                cur.execute(f"delete from {T_LOOKUPS} where vin = ?", (vin,))
                cur.execute(_insert(T_LOOKUPS, LOOKUP_COLS), [lookup[c] for c in LOOKUP_COLS])
                for table, cols, rows in ((T_INSPECTIONS, INSPECTION_COLS, inspections),
                                          (T_RECS, RECOMMENDATION_COLS, recs), (T_ASSOC, ASSOCIATE_COLS, associates)):
                    if rows:
                        cur.executemany(_insert(table, cols), [[r[c] for c in cols] for r in rows])
            self.con.commit()
        except Exception:
            self.con.rollback()
            raise

    def prune(self, keep_vins: list[str]) -> int:
        """Delete every VIN no longer in used inventory. Returns how many VINs were removed."""
        cur = self.con.cursor()
        try:
            cur.execute("create table #keep (vin char(17) primary key)")
            for i in range(0, len(keep_vins), 500):
                cur.executemany("insert into #keep (vin) values (?)", [[v] for v in keep_vins[i:i + 500]])
            gone = f"select vin from {T_LOOKUPS} where vin not in (select vin from #keep)"
            cur.execute(f"select count(*) from ({gone}) g")
            n = cur.fetchone()[0]
            cur.execute(f"delete from {T_ASSOC} where mpi_uuid in (select mpi_uuid from {T_INSPECTIONS} where vin in ({gone}))")
            cur.execute(f"delete from {T_RECS} where vin in ({gone})")
            cur.execute(f"delete from {T_INSPECTIONS} where vin in ({gone})")
            cur.execute(f"delete from {T_LOOKUPS} where vin in ({gone})")
            cur.execute("drop table #keep")
            self.con.commit()
            return n
        except Exception:
            self.con.rollback()
            raise
