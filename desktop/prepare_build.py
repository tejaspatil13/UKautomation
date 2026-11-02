"""Prepare a desktop build for one channel (run before PyInstaller; CI runs it on the Windows runner).

    python desktop/prepare_build.py --channel test --version 1.0.3-test.17 \\
        --license-url https://copilot-license-test.<you>.workers.dev --license-public-key <base64> \\
        --update-repo https://github.com/<owner>/<repo>

Writes desktop/build_config.json, build/duckdb_extensions.zip (DuckDB's httpfs for this platform) and
build/version_info.txt (Windows file version). Refuses mixed-up settings, e.g. a stable (client)
build pointed at the test licence server.
"""
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from desktop.config import LICENSE_ENV_FOR, BuildConfig  # noqa: E402

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?$")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--channel", required=True, choices=sorted(LICENSE_ENV_FOR))
    p.add_argument("--version", required=True)
    p.add_argument("--license-url", required=True)
    p.add_argument("--license-public-key", required=True)
    p.add_argument("--update-repo", required=True)
    args = p.parse_args()

    m = SEMVER.match(args.version)
    if not m:
        p.error(f"version {args.version!r} is not semantic (1.2.3 or 1.2.3-test.4)")
    if args.channel == "stable" and m.group(4):
        p.error("a stable (client) release can't have a pre-release version")
    if args.channel == "test" and not m.group(4):
        p.error("a test build needs a pre-release version, e.g. 1.0.1-test.5")
    cfg = BuildConfig(channel=args.channel, version=args.version, license_url=args.license_url.rstrip("/"),
                      license_public_key=args.license_public_key, license_env=LICENSE_ENV_FOR[args.channel],
                      update_repo=args.update_repo.rstrip("/"))
    cfg.validate()
    (ROOT / "desktop" / "build_config.json").write_text(json.dumps(cfg.__dict__, indent=2), encoding="utf-8")

    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    ext = build / "duckdb_extensions"
    import duckdb

    con = duckdb.connect()
    con.execute("SET extension_directory = '%s'" % str(ext).replace("'", "''"))
    con.execute("INSTALL httpfs")
    con.execute("LOAD httpfs")
    import shutil

    shutil.make_archive(str(build / "duckdb_extensions"), "zip", ext)  # zipped: PyInstaller leaves it alone

    major, minor, patch = (int(x) for x in m.groups()[:3])
    (build / "version_info.txt").write_text(VERSION_INFO.format(
        t=f"({major}, {minor}, {patch}, 0)", v=args.version), encoding="utf-8")
    print(f"prepared {cfg.channel} build {cfg.version}")
    return 0


VERSION_INFO = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={t}, prodvers={t}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080904B0', [
      StringStruct('CompanyName', 'Copilot'),
      StringStruct('FileDescription', 'Copilot'),
      StringStruct('FileVersion', '{v}'),
      StringStruct('InternalName', 'Copilot'),
      StringStruct('OriginalFilename', 'Copilot.exe'),
      StringStruct('ProductName', 'Copilot'),
      StringStruct('ProductVersion', '{v}')])]),
    VarFileInfo([VarStruct('Translation', [2057, 1200])])
  ]
)
"""

if __name__ == "__main__":
    raise SystemExit(main())
