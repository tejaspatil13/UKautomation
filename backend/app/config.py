"""Application settings, loaded from environment variables (prefix UKA_) or a .env file."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="UKA_", env_file=ROOT_DIR / ".env", extra="ignore")

    data_dir: Path = ROOT_DIR / "data"
    template_path: Path = ROOT_DIR / "templates" / "output_template.xlsx"
    frontend_dist: Path = ROOT_DIR / "frontend" / "dist"

    # VO rating list. Empty URL = discover the newest compiled-epoch baseline for voa_list_year.
    voa_list_url: str = ""
    voa_list_year: int = 2026

    # Automatic updates: check daily whether the VO or Overture has published new data, and install
    # it during the night window (local time, "HH-HH"). auto_import_on_start builds missing datasets
    # straight away on first boot.
    auto_refresh: bool = True
    refresh_check_hours: int = 24
    refresh_window: str = "01-05"
    auto_import_on_start: bool = False

    # Limits
    max_results_per_search: int = 5000
    max_concurrent_jobs: int = 2
    export_retention_hours: int = 72

    # Companies House (owner names). Empty = read the public website; a free API key is faster and
    # more reliable: https://developer.company-information.service.gov.uk/
    companies_house_api_key: str = ""

    # Optional shared password for the dashboard (HTTP Basic). Empty = no auth (local use only).
    app_password: str = ""

    log_level: str = "INFO"
    # Output of the dataset import subprocesses (desktop app: a file, shown as progress). Empty = inherit.
    import_log_path: Path | None = None

    @property
    def voa_db_path(self) -> Path:
        return self.data_dir / "voa.sqlite"

    @property
    def places_db_path(self) -> Path:
        return self.data_dir / "places.sqlite"

    @property
    def companies_db_path(self) -> Path:
        return self.data_dir / "companies.sqlite"

    @property
    def app_db_path(self) -> Path:
        return self.data_dir / "app.sqlite"

    @property
    def export_dir(self) -> Path:
        return self.data_dir / "exports"


@lru_cache
def get_settings() -> Settings:
    return Settings()
