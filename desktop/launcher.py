"""Copilot desktop app: double-click → the existing dashboard in its own window.

    Copilot.exe                          normal start
    Copilot.exe --run-import <module>    (internal) run a dataset import; replaces `python -m <module>`
    Copilot.exe --self-test              check the packaged app works (used by the Windows build)
    Copilot.exe --dev-url <url>          (test builds only) show a development server in the window

Start-up: Velopack hooks → logging → one copy only → backend settings → local server (127.0.0.1,
free port) → window on the start-up screen, which checks the licence, shows the first-time data
download and then opens the dashboard. Closing the window stops the server and any data import.
"""
import argparse
import logging
import os
import runpy
import sys
import threading

from desktop import logs, updater
from desktop.config import BuildConfig, ConfigError, Paths, backend_environment, frozen, resource_dir
from desktop.instance import SingleInstance

log = logging.getLogger("copilot")


def message_box(text: str) -> None:
    log.error(text)
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, text, "Copilot", 0x10)
    elif sys.stderr:
        print(text, file=sys.stderr)


def run_import_child(module: str) -> int:
    """`python -m <module>` for the packaged app. Output goes to the import log the parent opened."""
    if sys.stdout is None or sys.stderr is None:
        logs.setup(Paths.resolve().logs, "import.log")
    sys.argv = [module]
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except Exception:
        logging.getLogger(module).exception("import failed")
        return 1
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="Copilot", add_help=False)
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--dev-url")
    p.add_argument("--debug", action="store_true")
    args, _ = p.parse_known_args(argv)  # Velopack may add its own arguments
    return args


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not frozen():
        sys.path.insert(0, str(resource_dir() / "backend"))  # the existing backend's `app` package
    if argv[:1] == ["--run-import"] and len(argv) > 1:
        return run_import_child(argv[1])
    updater.run_hooks()
    args = parse_args(argv)

    paths = Paths.resolve()
    paths.create()
    logs.setup(paths.logs, console=not frozen())
    try:
        cfg = BuildConfig.load()
    except ConfigError as e:
        message_box(f"Copilot can't start: {e}")
        return 2
    log.info("Copilot %s (%s) starting; data in %s", cfg.version, cfg.channel, paths.data)

    if args.self_test:
        from desktop.selftest import run

        return run(cfg, paths)
    if args.dev_url:
        if cfg.channel == "stable":
            message_box("Development mode isn't available in this version of Copilot.")
            return 2
        return run_dev_window(args.dev_url, paths, args.debug)

    instance = SingleInstance(paths.home)
    if not instance.acquire():
        if not instance.focus_running():
            message_box("Copilot is already open.")
        return 0
    try:
        return run_app(cfg, paths, instance, args.debug)
    except Exception as e:
        log.exception("Copilot stopped because of an error")
        message_box(f"Copilot stopped because of an error:\n\n{e}\n\nThe details are in {paths.logs}.")
        return 1
    finally:
        instance.release()


def license_manager(cfg: BuildConfig, paths: Paths, on_change):
    from desktop.license import CHECK_EVERY_S, LicenseClient, LicenseManager, machine_fingerprint

    if not cfg.licensing:  # source runs only (BuildConfig refuses unlicensed test/stable builds)
        log.warning("licensing is OFF (development run)")
        return LicenseManager(None)
    client = LicenseClient(cfg.license_url, cfg.license_public_key, cfg.license_env,
                           machine_fingerprint(paths.license), paths.license / f"license-{cfg.license_env}.json",
                           cfg.version)
    return LicenseManager(client, on_change, CHECK_EVERY_S.get(cfg.channel, CHECK_EVERY_S["stable"]))


def run_app(cfg: BuildConfig, paths: Paths, instance: SingleInstance, debug: bool) -> int:
    import webview

    os.environ.update(backend_environment(paths))  # before the backend reads its settings
    import app.main
    from app import refresh
    from desktop.server import SESSION_HEADER, BackendServer, DesktopState, build_app, default_name

    window_ref: dict = {}
    base = {"url": ""}

    def on_license_change(state):
        log.info("licence state: %s", state.kind)
        if not state.allowed and (w := window_ref.get("w")):
            w.load_url(base["url"] + "/desktop/")

    lic = license_manager(cfg, paths, on_license_change)
    if lic.client is not None:  # every Excel download is recorded with the licence server (row limits)
        app.main.app.state.download_guard = lic.client.record_download
        lic.client.user_name = default_name()  # until the dashboard sends the name entered there
    upd = updater.Updater(cfg.update_repo, cfg.channel)
    restart = threading.Event()
    state = DesktopState(cfg.version, cfg.channel, lic, paths.logs, upd, focus_secret=instance.focus_secret)

    def make_app(port: int):
        state.port = port
        return build_app(app.main.app, state)

    server = BackendServer(make_app)
    server.start()
    base["url"] = f"http://127.0.0.1:{server.port}"
    instance.publish(server.port)
    log.info("backend listening on %s", base["url"])

    threading.Thread(target=lic.run, name="licence", daemon=True).start()
    threading.Thread(target=upd.run, name="updates", daemon=True).start()

    window = webview.create_window("Copilot", f"{base['url']}/desktop/start?token={state.session_token}",
                                   width=1360, height=900, min_size=(1024, 680), text_select=True)
    window_ref["w"] = window

    # The window also proves itself with a header on every request to the local server: WebView2 on
    # Windows didn't send the session cookie back (seen on a real PC and in CI). pywebview runs
    # request_sent handlers on a background thread unless the event is locked; locked, the header is
    # added before WebView2 sends the request.
    from webview.event import Event

    def add_session_header(request):
        if request.url.startswith(base["url"] + "/"):
            request.headers[SESSION_HEADER] = state.session_token

    window.events.request_sent = Event(window, True)
    window.events.request_sent += add_session_header

    def on_closing():
        if any(d.updating for d in getattr(app.main.app.state, "updates", [])):
            return window.create_confirmation_dialog(
                "Close Copilot?", "Copilot is still downloading data. If you close it now, the download starts "
                "again the next time you open Copilot. Your current data stays usable.")
        return True

    def focus():
        window.restore()
        window.show()
        window.on_top = True
        window.on_top = False

    def restart_to_update():
        restart.set()
        window.destroy()

    window.events.closing += on_closing
    state.on_focus = focus
    state.on_restart_to_update = restart_to_update

    webview.settings["ALLOW_DOWNLOADS"] = True  # Excel/zip downloads open a Save dialog
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True  # Google Maps, Gov.uk, Companies House links
    # private_mode=False + storage_path: the dashboard's saved name and tab survive restarts.
    webview.start(private_mode=False, storage_path=str(paths.webview), debug=debug)

    log.info("window closed; shutting down")
    lic.stop()
    upd.stop()
    refresh.stop_imports()
    server.stop()
    if restart.is_set():
        log.info("restarting to install update %s", upd.version)
        upd.apply_and_restart()
    else:
        upd.apply_on_exit()
    return 0


def run_dev_window(url: str, paths: Paths, debug: bool) -> int:
    """Developer testing on the Windows PC: the window shows the Mac's dev server (hot reload)."""
    import webview

    log.warning("development mode: showing %s", url)
    webview.create_window(f"Copilot (development: {url})", url, width=1360, height=900)
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.start(private_mode=False, storage_path=str(paths.home / "webview-dev"), debug=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
