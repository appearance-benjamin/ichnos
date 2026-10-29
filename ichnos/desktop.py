"""Ichnos as a desktop app: the local app in a window of its own, started from a shortcut with an icon.

`ichnos desktop` starts the app without a console and opens it in Edge (or Chrome) in app mode: a window with
no tabs or address bar, its own browser profile and the app's icon. Each open page reports itself to the app every
few seconds and says goodbye when it closes; when no page has been open for a short while, the app stops. If the
app is already running, a second start only opens another window.

`ichnos shortcuts` puts "Ichnos" on the desktop and in the Start menu (Windows), pointing at
`pythonw -m ichnos desktop` in this folder, with the icon in ichnos/assets.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from .config import ROOT, Settings

PORT = 8766
ASSETS = Path(__file__).resolve().parent / "assets"
ICON = ASSETS / "ichnos.ico"
GRACE_S = 8            # how long the app waits after the last page closed (a reload closes and reopens)
# A page not heard from for this long is taken to be gone (a crashed window). Long, because browsers slow or freeze
# the timers of a window in the background, and a sleeping window must not stop the app under it.
STALE_S = 4 * 3600
NO_PAGE_S = 120        # if no page ever opens, stop after this long


def running(port: int = PORT) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/hello", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == "ichnos"
    except (OSError, ValueError):
        return False


def window_browser() -> str | None:
    """Edge first on Windows (always installed), then Chrome or Chromium."""
    from .document import find_browser
    edges = [os.path.expandvars(p) for p in (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                                             r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")]
    return next((e for e in edges if os.path.exists(e)), None) or shutil.which("msedge") or find_browser()


def open_window(url: str, root: Path = ROOT) -> subprocess.Popen | None:
    browser = window_browser()
    if not browser:
        webbrowser.open(url)
        return None
    profile = root / ".app-window"            # the window's own profile: its size and zoom are remembered
    return subprocess.Popen([browser, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run",
                             "--no-default-browser-check", "--disable-sync", "--window-size=1440,920"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _quiet_output(root: Path) -> None:
    """Started with pythonw there is no console: send output to a log file instead."""
    if sys.stdout is None or sys.stderr is None:
        log = root / "output" / "app.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        stream = open(log, "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or stream
        sys.stderr = sys.stderr or stream


def run(settings: Settings, root: Path = ROOT, reviewer: str | None = None, port: int = PORT) -> None:
    from .app import App, AppServer, handler_for
    _quiet_output(root)
    if running(port):
        open_window(f"http://127.0.0.1:{port}/", root)
        return
    app = App(settings, root, reviewer)
    server = None
    for candidate in range(port, port + 25):
        try:
            server = AppServer(("127.0.0.1", candidate), handler_for(app))
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"No free port between {port} and {port + 24}.")
    app.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Ichnos window app on http://127.0.0.1:{app.port}/")
    open_window(f"http://127.0.0.1:{app.port}/", root)
    started, empty_since = time.time(), None
    try:
        while True:
            time.sleep(2)
            now = time.time()
            with app.lock:
                for page, seen in list(app.pages.items()):
                    if now - seen > STALE_S:
                        del app.pages[page]
                open_pages = len(app.pages)
            if open_pages:
                empty_since = None
            elif app.ever_paged:
                empty_since = empty_since or now
                if now - empty_since > GRACE_S and not any(j["status"] == "running" for j in app.jobs):
                    break
            elif now - started > NO_PAGE_S:
                break
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Ichnos stopped")


# Shortcuts (Windows)

def shortcuts(root: Path = ROOT, desktop: bool = True, start_menu: bool = True) -> list[Path]:
    if os.name != "nt":
        raise SystemExit("Shortcuts are made on Windows only; elsewhere run: ichnos desktop")
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else (shutil.which("pythonw") and Path(shutil.which("pythonw"))) or exe
    places = []
    if desktop:
        places.append("Desktop")
    if start_menu:
        places.append("Programs")
    made = []
    for place in places:
        script = (
            "$dir = [Environment]::GetFolderPath('" + place + "'); "
            "$lnk = Join-Path $dir 'Ichnos.lnk'; "
            "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk); "
            f"$s.TargetPath = '{target}'; "
            "$s.Arguments = '-m ichnos desktop'; "
            f"$s.WorkingDirectory = '{root}'; "
            f"$s.IconLocation = '{ICON},0'; "
            "$s.Description = 'Ichnos: auditable AI-assisted scoping reviews'; "
            "$s.Save(); Write-Output $lnk")
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, check=True)
        made.append(Path(out.stdout.strip().splitlines()[-1]))
    return made


def main() -> None:
    """Entry point of the `ichnos-app` command (a GUI script: no console window)."""
    from .config import load_settings
    settings = load_settings()
    run(settings, ROOT, settings.reviewer)
