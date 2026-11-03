# PyInstaller build of the Copilot desktop app:  pyinstaller desktop/copilot.spec
# One folder (not one file): starts faster, and Velopack packages and updates a folder.
# Expects, before building: frontend/dist (npm run build), desktop/build_config.json (CI writes it),
# and optionally build/duckdb_extensions.zip (DuckDB's httpfs extension; see desktop/prepare_build.py).
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent
BACKEND = ROOT / "backend"

datas = [
    (str(ROOT / "frontend" / "dist"), "frontend/dist"),
    (str(ROOT / "templates" / "output_template.xlsx"), "templates"),
    (str(BACKEND / "app" / "voa" / "description_codes.json"), "app/voa"),
    (str(BACKEND / "app" / "companies" / "labels.json"), "app/companies"),
    (str(ROOT / "desktop" / "pages"), "desktop/pages"),
    (str(ROOT / "desktop" / "VERSION"), "desktop"),
]
if (ROOT / "desktop" / "build_config.json").exists():
    datas.append((str(ROOT / "desktop" / "build_config.json"), "desktop"))
if (ROOT / "build" / "duckdb_extensions.zip").exists():
    datas.append((str(ROOT / "build" / "duckdb_extensions.zip"), "."))
datas += collect_data_files("tzdata")  # Windows has no time-zone database of its own
datas += collect_data_files("phonenumbers")
datas += collect_data_files("webview")

hiddenimports = [
    # run as `Copilot.exe --run-import <module>`: nothing imports them statically
    "app.voa.importer", "app.contacts.overture_import", "app.companies.bulk_import", "desktop.selftest_child",
    *collect_submodules("app"),
    *collect_submodules("uvicorn"),
    *collect_submodules("phonenumbers"),
    "clr",  # pythonnet, for pywebview's Windows window
]

a = Analysis(
    [str(ROOT / "desktop" / "__main__.py")],
    pathex=[str(ROOT), str(BACKEND)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "matplotlib", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = ROOT / "desktop" / "assets" / {"win32": "copilot.ico", "darwin": "copilot.icns"}.get(sys.platform, "copilot.png")
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Copilot",
    console=False,  # a windowed app: no black console window
    icon=str(icon) if icon.exists() else None,
    version=str(ROOT / "build" / "version_info.txt") if (ROOT / "build" / "version_info.txt").exists() else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Copilot", upx=False)

if sys.platform == "darwin":  # macOS: a normal Copilot.app (Velopack packs and updates the bundle)
    import json

    config = ROOT / "desktop" / "build_config.json"
    version = json.loads(config.read_text())["version"] if config.exists() else "0.0.0"
    short = version.split("-")[0]
    app = BUNDLE(
        coll,
        name="Copilot.app",
        icon=str(icon),
        bundle_identifier="com.tejaspatil.copilot",
        version=short,
        info_plist={
            "CFBundleDisplayName": "Copilot",
            "CFBundleShortVersionString": short,
            "CFBundleVersion": version,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
        },
    )
