"""Contact provider backed by the locally imported places DB (Overture Maps).

Candidates are every place with the premises' postcode, plus places that have no postcode but
lie within ~75 m of the postcode's centroid (estimated from the places that do carry it).
No network calls, no quotas — lookups are a few indexed SQLite reads.
"""
import asyncio
import math
import os
import sqlite3
from functools import lru_cache
from pathlib import Path

from app.contacts.base import ContactCandidate
from app.models import PropertyRecord

NEARBY_M = 75
_DEG_LAT_M = 111_320


class LocalPlacesProvider:
    name = "overture"

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._open()

    def _open(self) -> None:
        self._inode = os.stat(self.db_path).st_ino
        self._con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._for_postcode.cache_clear()

    async def candidates(self, record: PropertyRecord) -> list[ContactCandidate]:
        if not record.postcode:
            return []
        async with self._lock:  # one shared read-only connection; lookups take well under 1 ms
            if self._con is None:  # closed so a refresh could swap in a new file
                self._open()
            elif os.stat(self.db_path).st_ino != self._inode:  # a refresh swapped in a new file
                self._con.close()
                self._open()
            return list(self._for_postcode(record.postcode))

    def close(self) -> None:
        """Release the file (Windows can't replace an open file). Called on the event loop, so it never
        interrupts a lookup; the next lookup opens the file again."""
        if self._con is not None:
            self._con.close()
            self._con = None

    @lru_cache(maxsize=8192)
    def _for_postcode(self, postcode: str) -> tuple[ContactCandidate, ...]:
        rows = self._con.execute("SELECT * FROM places WHERE postcode = ?", (postcode,)).fetchall()
        located = [r for r in rows if r["lat"] is not None]
        nearby = []
        if located:
            lat = sum(r["lat"] for r in located) / len(located)
            lon = sum(r["lon"] for r in located) / len(located)
            dlat = NEARBY_M / _DEG_LAT_M
            dlon = dlat / max(math.cos(math.radians(lat)), 0.1)
            for r in self._con.execute(
                "SELECT * FROM places WHERE postcode = '' AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                (lat - dlat, lat + dlat, lon - dlon, lon + dlon),
            ):
                dist = math.hypot((r["lat"] - lat) * _DEG_LAT_M, (r["lon"] - lon) * _DEG_LAT_M * math.cos(math.radians(lat)))
                if dist <= NEARBY_M:
                    nearby.append(_to_candidate(r, distance_m=round(dist)))
        return tuple([_to_candidate(r) for r in rows] + nearby)


def _to_candidate(r: sqlite3.Row, distance_m: float | None = None) -> ContactCandidate:
    address = ", ".join(p for p in (r["freeform"], r["locality"]) if p and p.strip())
    explore = f"https://explore.overturemaps.org/#18/{r['lat']:.6f}/{r['lon']:.6f}" if r["lat"] is not None else ""
    return ContactCandidate(
        name=r["name"],
        address=address,
        postcode=r["postcode"] or "",
        phone=r["phone"] or "",
        website=r["website"] or "",
        categories=[r["category"]] if r["category"] else [],
        closed=(r["operating_status"] or "") == "permanently_closed",
        source=f"overture ({r['sources']})" if r["sources"] else "overture",
        source_url=explore,
        source_id=r["id"],
        source_confidence=r["confidence"],
        distance_m=distance_m,
    )
