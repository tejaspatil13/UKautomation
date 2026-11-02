"""Build configuration and where the desktop app keeps its files.

build_config.json is written by the Windows build (CI) for one release channel and packaged inside
the app. It holds only public values: the licence server URL, its *public* verification key and
the update source. Source runs without it are "dev" builds.

Channels:
    dev     running from source on a developer machine (licensing optional, never distributed)
    test    builds for the developer's Windows test PC (test licence server, test update channel)
    stable  builds for the client (production licence server, stable update channel)
"""
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "Copilot"
CHANNELS = ("dev", "test", "stable")
LICENSE_ENV_FOR = {"test": "test", "stable": "production"}


class ConfigError(RuntimeError):
    pass


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Files packaged with the app (PyInstaller's folder), or the repository root when run from source."""
    if frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BuildConfig:
    channel: str = "dev"
    version: str = "0.0.0-dev"
    license_url: str = ""
    license_public_key: str = ""  # base64 raw Ed25519 public key
    license_env: str = ""  # "test" or "production": tokens from the other server are rejected
    update_repo: str = ""  # https://github.com/<owner>/<repo>

    @property
    def licensing(self) -> bool:
        return bool(self.license_url)

    @classmethod
    def load(cls, path: Path | None = None) -> "BuildConfig":
        path = path or resource_dir() / "desktop" / "build_config.json"
        if not path.exists():
            if frozen():
                raise ConfigError("This copy of Copilot was built without its configuration.")
            return cls(version=_source_version())
        data = json.loads(path.read_text(encoding="utf-8"))
        cfg = cls(**{k: str(v) for k, v in data.items() if k in cls.__dataclass_fields__})
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.channel not in CHANNELS:
            raise ConfigError(f"Unknown release channel {self.channel!r}.")
        if frozen() and self.channel == "dev":
            raise ConfigError("Development builds can't be run as an installed app.")
        if self.channel in LICENSE_ENV_FOR:
            # A test build must never talk to the production licence server, and the reverse.
            if not (self.license_url and self.license_public_key):
                raise ConfigError(f"The {self.channel} build has no licence server configured.")
            if self.license_env != LICENSE_ENV_FOR[self.channel]:
                raise ConfigError(f"The {self.channel} build points at the {self.license_env or 'unknown'} licence server.")


def _source_version() -> str:
    return (Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip() or "0.0.0") + "-dev"


@dataclass(frozen=True)
class Paths:
    home: Path  # %LOCALAPPDATA%\Copilot

    @classmethod
    def resolve(cls) -> "Paths":
        if override := os.environ.get("COPILOT_HOME"):
            return cls(Path(override))
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
        return cls(base / APP_NAME)

    @property
    def data(self) -> Path:
        # COPILOT_DATA_DIR lets a developer point the app at existing datasets instead of downloading 4.4 GB.
        return Path(os.environ["COPILOT_DATA_DIR"]) if os.environ.get("COPILOT_DATA_DIR") else self.home / "data"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def webview(self) -> Path:
        return self.home / "webview"

    @property
    def license(self) -> Path:
        return self.home / "license"

    def create(self) -> None:
        for d in (self.home, self.data, self.logs, self.webview, self.license):
            d.mkdir(parents=True, exist_ok=True)


def backend_environment(paths: Paths) -> dict[str, str]:
    """Settings for the existing backend (app.config, UKA_ prefix). Set before app.main is imported."""
    res = resource_dir()
    env = {
        "UKA_DATA_DIR": str(paths.data),
        "UKA_FRONTEND_DIST": str(res / "frontend" / "dist"),
        "UKA_TEMPLATE_PATH": str(res / "templates" / "output_template.xlsx"),
        "UKA_AUTO_IMPORT_ON_START": "true",  # first launch downloads the datasets (existing behaviour on the server)
        "UKA_APP_PASSWORD": "",  # one user on their own PC, reachable only from this PC: no dashboard password
        "UKA_IMPORT_LOG_PATH": str(paths.logs / "import.log"),
    }
    if ext := duckdb_extensions(res, paths):
        env["UKA_DUCKDB_EXTENSIONS"] = str(ext)
    return env


def duckdb_extensions(res: Path, paths: Paths) -> Path | None:
    """DuckDB's httpfs extension (for the Overture import) ships zipped with the app, so it works where
    downloads from extensions.duckdb.org are blocked. Unpacked once per app version."""
    bundle = res / "duckdb_extensions.zip"
    if not bundle.exists():
        return None
    import hashlib
    import zipfile

    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()[:12]
    target = paths.home / "duckdb_extensions" / digest
    if not target.is_dir():
        tmp = target.with_name(digest + ".tmp")
        with zipfile.ZipFile(bundle) as zf:
            zf.extractall(tmp)
        tmp.replace(target)
    return target
