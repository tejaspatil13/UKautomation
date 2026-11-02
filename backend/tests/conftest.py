"""Shared fixtures. Everything here is synthetic or mocked — no test touches the network."""
import zipfile
from pathlib import Path

import pytest

from app.config import ROOT_DIR, Settings
from app.voa.importer import import_zip

TEMPLATE = ROOT_DIR / "templates" / "output_template.xlsx"


def vo_line(n, uarn, number, street, town, postcode, rv, desc="SHOP AND PREMISES", firm="", district="", code="CS"):
    """One list-entry record in the VO asterisk-delimited layout (28 fields)."""
    full = ", ".join(p for p in (number, street, district, town) if p)
    f = [""] * 28
    f[0], f[1], f[3], f[4], f[5], f[6], f[7], f[8] = str(n), "4610", f"REF{n}", code, desc, uarn, full, firm
    f[9], f[10], f[11], f[12], f[14], f[17], f[19] = number, street, town, district, postcode, str(rv) if rv is not None else "", str(n)
    return "*".join(f)


SAMPLE_LINES = [
    vo_line(1, "300675222", "13", "TRINITY STREET", "COVENTRY", "CV1 1FJ", 22000),
    vo_line(2, "113420222", "15", "TRINITY STREET", "COVENTRY", "CV1 1FJ", 10750, "HAIRDRESSING SALON AND PREMISES",
            code="CS3"),
    vo_line(3, "235950222", "9-11", "TRINITY STREET", "COVENTRY", "CV1 1FJ", 43750),
    vo_line(4, "3529303000", "ADVERT RIGHT ON BUS SHELTER", "TRINITY STREET", "COVENTRY", "CV1 1FJ", 15000,
            "ADVERTISING RIGHT AND PREMISES", code="CA"),
    # "CR1O" = primary code CR1 (cafe) + secondary code O, as stored in the rating list
    vo_line(5, "11602154000", "27-29", "TRINITY STREET", "COVENTRY", "CV1 1FJ", 46000, "CAFE AND PREMISES", code="CR1O"),
    vo_line(6, "999000001", "1", "LOWER HOLYHEAD ROAD", "COVENTRY", "CV1 3AX", 19500, "RESTAURANT AND PREMISES",
            code="CR"),
    vo_line(7, "999000002", "PT 1", "LOWER HOLYHEAD ROAD", "COVENTRY", "CV1 3AX", 14000),
    vo_line(8, "999000003", "69", "LODGE LANE", "GRAYS", "RM17 5RZ", 17000, firm="GRAYS EXPRESS LTD"),
    vo_line(9, "999000004", "", "THE HORTON INN, CRANBORNE ROAD", "WIMBORNE", "BH21 5AD", 28300,
            "PUBLIC HOUSE AND PREMISES", district="HORTON", code="CL"),
    vo_line(10, "999000005", "5", "DELETED ROW", "COVENTRY", "CV1 1AA", None),  # deleted entry, null RV
    "garbage line without enough fields",
]


@pytest.fixture
def vo_zip(tmp_path: Path) -> Path:
    path = tmp_path / "vo.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("uk-englandwales-ndr-2026-listentries-test-csv.csv", "\n".join(SAMPLE_LINES) + "\n")
        zf.writestr("uk-englandwales-ndr-2026-listentries-test-historic-csv.csv", "ignored\n")
    return path


@pytest.fixture
def settings(tmp_path: Path, vo_zip: Path) -> Settings:
    s = Settings(
        data_dir=tmp_path / "data", template_path=TEMPLATE, frontend_dist=tmp_path / "nofrontend",
        auto_refresh=False, auto_import_on_start=False,
    )
    s.data_dir.mkdir(parents=True)
    import_zip(vo_zip, s.voa_db_path, list_year=2026)
    return s
