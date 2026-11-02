"""Entry point: start the local backend and open the UI in an app window."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import webbrowser

from . import __version__
from .server import IDLE_TIMEOUT, Library, start_server
from .store import config_dir

CHROMIUM_NAMES = [
    "chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave-browser",
    "brave", "microsoft-edge", "microsoft-edge-stable", "vivaldi", "vivaldi-stable",
]


def find_chromium() -> str | None:
    """A Chromium-based browser can open the UI as a chromeless app window."""
    for name in CHROMIUM_NAMES:
        p = shutil.which(name)
        if p:
            return p
    if sys.platform == "win32":
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if not base:
                continue
            for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                        r"BraveSoftware\Brave-Browser\Application\brave.exe"):
                p = os.path.join(base, rel)
                if os.path.exists(p):
                    return p
    if sys.platform == "darwin":
        for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"):
            if os.path.exists(p):
                return p
    return None


def run_webview(url: str) -> bool:
    try:
        import webview  # type: ignore  # optional: pip install pywebview
    except ImportError:
        return False
    webview.create_window("SteamSorter", url, width=1440, height=900, min_size=(900, 600),
                          background_color="#07090e")
    webview.start()
    return True


def run_app_window(url: str) -> bool:
    exe = find_chromium()
    if not exe:
        return False
    profile = config_dir() / "window-profile"
    args = [exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,900",
            "--no-first-run", "--no-default-browser-check", "--class=SteamSorter"]
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return False
    start = time.monotonic()
    proc.wait()
    # Some browsers hand the window to an already-running instance and exit at once;
    # in that case fall back to waiting for the page to go away.
    return time.monotonic() - start > 5


def wait_for_idle(lib: Library) -> None:
    lib.last_ping = time.monotonic()
    try:
        while time.monotonic() - lib.last_ping < IDLE_TIMEOUT:
            time.sleep(2)
    except KeyboardInterrupt:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(prog="steamsorter", description="Sort your Steam library.")
    ap.add_argument("--browser", action="store_true", help="open in your normal browser instead of an app window")
    ap.add_argument("--no-open", action="store_true", help="just print the URL and keep serving")
    ap.add_argument("--port", type=int, default=0, help="port to listen on (default: random)")
    ap.add_argument("--version", action="version", version=f"SteamSorter {__version__}")
    a = ap.parse_args()

    lib = Library()
    httpd, url = start_server(lib, a.port)
    if lib.cfg.steam_id:
        lib.refresh()
    print(f"SteamSorter {__version__} running at {url}  (Ctrl+C to quit)", flush=True)
    try:
        if a.no_open:
            while True:
                time.sleep(3600)
        elif a.browser or not (run_webview(url) or run_app_window(url)):
            webbrowser.open(url)
            wait_for_idle(lib)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        lib.close()


if __name__ == "__main__":
    main()
