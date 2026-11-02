"""One row per business: VO entries that matched the same business and number are merged.

A shared building often has several rateable entries (floors, units) that all resolve to the same
business. Callers want that business once, with every rateable value and Gov.uk record listed.
"""
from app.excel import ExportRow
from app.models import ResultRecord
from app.normalize import name_key


def business_key(rec: ResultRecord, merge_shared: bool = False) -> str:
    """Rows with the same key are one business.

    In a shared building (business centre) every unit gets the same "most likely" business, so its
    units are only merged when asked (the owner search wants one lookup per business); for display
    each unit stays a row of its own.
    """
    if rec.business_name and rec.phone and (merge_shared or not rec.shared_building):
        return f"{name_key(rec.business_name)}|{''.join(ch for ch in rec.phone if ch.isdigit())}"
    return f"uarn:{rec.uarn}"


def group_rows(rows: list[ExportRow], merge_shared: bool = False) -> list[ExportRow]:
    """Merge rows for the same business, keeping first-seen order; highest RV first within a group."""
    groups: dict[str, ExportRow] = {}
    for row in rows:
        key = business_key(row.record, merge_shared)
        if key in groups:
            groups[key].properties.append(row.record)
        else:
            groups[key] = ExportRow(row.record, row.search, row.added, [row.record], key=key)
    for g in groups.values():
        g.properties.sort(key=lambda r: -r.rv)
        g.record = g.properties[0]
    return list(groups.values())


def group_records(records: list[ResultRecord], search: str = "") -> list[ExportRow]:
    return group_rows([ExportRow(r, search) for r in records])


def row_json(row: ExportRow) -> dict:
    """API shape of a grouped row: the main record plus every VO entry for the business."""
    data = row.record.model_dump(mode="json")
    data |= {
        "key": row.key,
        "search": row.search,
        "added": row.added.isoformat() if row.added else None,
        "uarns": [p.uarn for p in row.properties],
        "properties": [
            {"uarn": p.uarn, "rv": p.rv, "address": p.address, "description": p.description, "source_url": p.source_url}
            for p in row.properties
        ],
    }
    if row.company:
        data["company"] = row.company.model_dump(mode="json")
    return data


def split(rows: list, parts: int) -> list[list]:
    """Divide rows into `parts` consecutive chunks whose sizes differ by at most one."""
    parts = max(1, min(parts, len(rows)))
    size, extra = divmod(len(rows), parts)
    out, start = [], 0
    for i in range(parts):
        end = start + size + (1 if i < extra else 0)
        out.append(rows[start:end])
        start = end
    return out
