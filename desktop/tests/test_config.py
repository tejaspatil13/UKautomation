"""Build configuration safety, data locations, single instance, logging."""
import json
import sys

import pytest

from desktop import logs
from desktop.config import BuildConfig, ConfigError, Paths, backend_environment
from desktop.instance import SingleInstance

GOOD = {"license_url": "https://lic.example", "license_public_key": "AAAA", "update_repo": "https://github.com/o/r"}


def write(tmp_path, **cfg):
    p = tmp_path / "build_config.json"
    p.write_text(json.dumps(cfg))
    return p


def test_test_and_stable_builds_load(tmp_path):
    assert BuildConfig.load(write(tmp_path, channel="test", license_env="test", **GOOD)).licensing
    assert BuildConfig.load(write(tmp_path, channel="stable", license_env="production", **GOOD)).channel == "stable"


@pytest.mark.parametrize("cfg", [
    {"channel": "stable", "license_env": "test", **GOOD},         # client build on the test licence server
    {"channel": "test", "license_env": "production", **GOOD},     # test build on the production server
    {"channel": "stable", "license_env": "production"},            # client build without licensing
    {"channel": "beta", "license_env": "test", **GOOD},
])
def test_mixed_up_builds_refuse_to_start(tmp_path, cfg):
    with pytest.raises(ConfigError):
        BuildConfig.load(write(tmp_path, **cfg))


def test_packaged_app_needs_its_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(ConfigError):
        BuildConfig.load(tmp_path / "missing.json")
    with pytest.raises(ConfigError):
        BuildConfig.load(write(tmp_path, channel="dev"))


def test_source_run_is_a_dev_build(tmp_path):
    cfg = BuildConfig.load(tmp_path / "missing.json")
    assert cfg.channel == "dev" and not cfg.licensing and cfg.version.endswith("-dev")


def test_data_lives_outside_the_install_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("COPILOT_DATA_DIR", raising=False)
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "Copilot"))
    paths = Paths.resolve()
    env = backend_environment(paths)
    assert env["UKA_DATA_DIR"] == str(tmp_path / "Copilot" / "data")
    assert env["UKA_APP_PASSWORD"] == "" and env["UKA_AUTO_IMPORT_ON_START"] == "true"
    assert env["UKA_IMPORT_LOG_PATH"].endswith("import.log")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path")
def test_windows_default_location(monkeypatch, tmp_path):
    monkeypatch.delenv("COPILOT_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert Paths.resolve().data == tmp_path / "Copilot" / "data"


def test_only_one_copy_runs(tmp_path):
    first, second = SingleInstance(tmp_path), SingleInstance(tmp_path)
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_log_helpers(tmp_path):
    (tmp_path / "copilot.log").write_text("one\n2026 INFO x imported 1,000 rows\n\n", encoding="utf-8")
    assert logs.last_line(tmp_path / "copilot.log").endswith("imported 1,000 rows")
    assert logs.last_line(tmp_path / "nope.log") == ""
    assert logs.diagnostics_zip(tmp_path)[:2] == b"PK"
