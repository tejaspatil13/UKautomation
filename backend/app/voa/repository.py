"""Read-only queries against the imported VO rating list.

The imported file is the *current* compiled list: every entry is listed and has a rateable value.
Properties the VO site shows as "removed from the rating list" are not in it, so they are skipped
automatically.
"""
import sqlite3
from functools import cached_property
from pathlib import Path

from app.models import PropertyRecord, SearchRequest
from app.normalize import clean_ws, parse_postcode_query, smart_title
from app.voa.description_codes import primary_code


class DatasetMissingError(RuntimeError):
    pass


class VoaRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._town_outward_cache: dict[str, list[tuple[str, int]]] = {}

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.exists():
            raise DatasetMissingError(
                "The VO rating list has not been imported yet. Run: python -m app.voa.importer"
            )
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def meta(self) -> dict[str, str]:
        with self._connect() as con:
            return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}

    @cached_property
    def _db_codes_by_primary(self) -> dict[str, list[str]]:
        """Site code -> every rating-list code that belongs to it ("CW" -> ["CW", "CWO", "CWS", ...])."""
        with self._connect() as con:
            db_codes = [r[0] for r in con.execute("SELECT DISTINCT desc_code FROM properties")]
        out: dict[str, list[str]] = {}
        for code in db_codes:
            if primary := primary_code(code):
                out.setdefault(primary, []).append(code)
        return out

    # ---------- suggestions for the Town / Postcode boxes ----------
    @cached_property
    def _places(self) -> list[tuple[str, int]]:
        """Every town and locality in the list with its property count, biggest first (~20k names)."""
        counts: dict[str, int] = {}
        with self._connect() as con:
            for sql in ("SELECT town, COUNT(*) FROM properties WHERE town != '' GROUP BY town",
                        "SELECT postal_district, COUNT(*) FROM properties WHERE postal_district != '' GROUP BY postal_district"):
                for name, n in con.execute(sql):
                    counts[name] = counts.get(name, 0) + n
        return sorted(counts.items(), key=lambda kv: -kv[1])

    def suggest_towns(self, q: str, limit: int = 50) -> list[dict]:
        q = clean_ws(q).upper()
        if not q:
            hits = self._places[:limit]
        else:
            starts = [p for p in self._places if p[0].startswith(q)]
            contains = [p for p in self._places if q in p[0] and not p[0].startswith(q)] if len(starts) < limit else []
            hits = (starts + contains)[:limit]
        return [{"value": smart_title(name), "count": n} for name, n in hits]

    @cached_property
    def _outwards(self) -> list[tuple[str, int]]:
        with self._connect() as con:
            rows = con.execute("SELECT outward, COUNT(*) FROM properties WHERE outward IS NOT NULL GROUP BY outward").fetchall()
        return sorted(rows, key=lambda r: _postcode_sort_key(r[0]))

    def _town_outwards(self, town: str) -> list[tuple[str, int]]:
        if town not in self._town_outward_cache:
            with self._connect() as con:
                rows = con.execute(
                    "SELECT outward, COUNT(*) FROM properties WHERE (town = ? OR postal_district = ?) "
                    "AND outward IS NOT NULL GROUP BY outward", (town, town),
                ).fetchall()
            total = sum(n for _, n in rows) or 1
            # Drop stray districts (a handful of mis-filed entries), keep the town's real postcode areas.
            rows = [r for r in rows if r[1] >= max(2, total * 0.0005)]
            self._town_outward_cache[town] = sorted(rows, key=lambda r: _postcode_sort_key(r[0]))
        return self._town_outward_cache[town]

    def suggest_postcodes(self, town: str, q: str, limit: int = 60) -> list[dict]:
        """Postcode districts ("CV1"), sectors ("CV1 1") or full postcodes ("CV1 1FJ") matching what was typed.

        With a town: only postcodes in that town. Typing more narrows from district to sector to postcode.
        """
        town = clean_ws(town).upper()
        q = clean_ws(q).upper()
        if " " not in q:
            pool = self._town_outwards(town) if town else self._outwards
            hits = [r for r in pool if r[0].startswith(q)] if q else pool
            if not (len(hits) == 1 and hits[0][0] == q):
                return [{"value": v, "count": n} for v, n in hits[:limit]]
            q += " "  # a complete district ("CV1"): offer its sectors next

        column = "sector" if len(q.split(" ", 1)[1]) <= 1 else "postcode"
        where = ["postcode >= ?", "postcode < ?"]
        params: list = [q, q + "￿"]
        if town:
            where.append("(town = ? OR postal_district = ?)")
            params += [town, town]
        sql = (f"SELECT {column}, COUNT(*) FROM properties WHERE {' AND '.join(where)} "
               f"GROUP BY {column} ORDER BY {column} LIMIT ?")
        with self._connect() as con:
            rows = con.execute(sql, [*params, limit]).fetchall()
            if column == "sector" and len(rows) == 1 and rows[0][0] == q:  # a complete sector: list its postcodes
                rows = con.execute(sql.replace("sector", "postcode"), [*params, limit]).fetchall()
        return [{"value": v, "count": n} for v, n in rows]

    def warm_up(self) -> None:
        """Build the suggestion caches (about a second) so the first keystroke is instant."""
        try:
            self._places, self._outwards  # noqa: B018
        except DatasetMissingError:
            pass

    def town_exists(self, town: str) -> bool:
        t = clean_ws(town).upper()
        with self._connect() as con:
            return con.execute(
                "SELECT 1 FROM properties WHERE town = ? OR postal_district = ? LIMIT 1", (t, t)
            ).fetchone() is not None

    def search(self, req: SearchRequest, limit: int) -> tuple[list[PropertyRecord], bool]:
        """Return (records, truncated). Records are ordered by postcode, street, number."""
        where, params = [], []
        if req.postcode:
            kind, pc = parse_postcode_query(req.postcode)  # validated by SearchRequest
            column = {"full": "postcode", "sector": "sector", "outward": "outward"}[kind]
            where.append(f"{column} = ?")
            params.append(pc)
        if req.town:
            t = req.town.upper()
            where.append("(town = ? OR postal_district = ?)")
            params += [t, t]
        if req.description_code:
            codes = self._db_codes_by_primary.get(req.description_code, [req.description_code])
            where.append(f"desc_code IN ({','.join('?' * len(codes))})")
            params += codes
        if req.rv_from is not None:
            where.append("rv >= ?")
            params.append(req.rv_from)
        if req.rv_to is not None:
            where.append("rv <= ?")
            params.append(req.rv_to)

        sql = (
            _SELECT + " WHERE " + " AND ".join(where) +
            " ORDER BY postcode, street, CAST(number_or_name AS INTEGER), number_or_name LIMIT ?"
        )
        with self._connect() as con:
            rows = con.execute(sql, [*params, limit + 1]).fetchall()
        truncated = len(rows) > limit
        return [_to_record(r) for r in rows[:limit]], truncated

    def get_many(self, uarns: list[str]) -> dict[str, PropertyRecord]:
        out: dict[str, PropertyRecord] = {}
        with self._connect() as con:
            for i in range(0, len(uarns), 500):
                chunk = uarns[i:i + 500]
                sql = f"{_SELECT} WHERE uarn IN ({','.join('?' * len(chunk))})"
                out.update({r["uarn"]: _to_record(r) for r in con.execute(sql, chunk)})
        return out


_SELECT = (
    "SELECT uarn, full_address, firm_name, number_or_name, street, town, postal_district, "
    "postcode, desc_text, rv FROM properties"
)


def _to_record(r: sqlite3.Row) -> PropertyRecord:
    postcode = r["postcode"] or ""
    full = clean_ws(r["full_address"])
    address = smart_title(full) + (f", {postcode}" if postcode and postcode not in full else "")
    return PropertyRecord(
        uarn=r["uarn"],
        address=address,
        street=smart_title(r["street"]),
        number_or_name=smart_title(r["number_or_name"]),
        town=smart_title(r["town"] or r["postal_district"]),
        postcode=postcode,
        description=smart_title(r["desc_text"]),
        rv=r["rv"],
        firm_name=smart_title(r["firm_name"]),
    )


def _postcode_sort_key(pc: str) -> tuple:
    """Natural order: CV1, CV2, ... CV10 rather than CV1, CV10, CV2."""
    import re

    m = re.match(r"([A-Z]+)(\d+)([A-Z]?)\s*(\d*)(.*)", pc or "")
    if not m:
        return (pc or "", 0, "", -1, "")
    area, num, letter, sector, unit = m.groups()
    return (area, int(num), letter, int(sector) if sector else -1, unit)
