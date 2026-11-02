"""The VO "Description code" options, exactly as offered by the Find-a-property advanced search."""
import json
from functools import lru_cache
from pathlib import Path

_FILE = Path(__file__).with_name("description_codes.json")


@lru_cache
def description_codes() -> dict[str, str]:
    """code -> label, e.g. "CS" -> "CS - Shop and Premises" (117 codes, site order)."""
    return {o["code"]: o["label"] for o in json.loads(_FILE.read_text(encoding="utf-8"))}


def primary_code(db_code: str) -> str | None:
    """Map a rating-list code to the site's code.

    The rating list stores primary + optional one-letter secondary code ("CWO" = "CW" + "O"),
    the site filters on the primary code only.
    """
    codes = description_codes()
    if db_code in codes:
        return db_code
    if len(db_code) > 1 and db_code[-1].isalpha() and db_code[:-1] in codes:
        return db_code[:-1]
    return None
