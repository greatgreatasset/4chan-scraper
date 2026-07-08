"""Auto-managed FlareSolverr companion.

archived.moe / thebarchive / archiveofsins sit behind a Cloudflare JS
challenge that only a real browser can pass. FlareSolverr solves it in a
headless Chrome and hands back clearance cookies. This module makes that
zero-setup: reuse a running instance if there is one, otherwise launch the
copy in ./flaresolverr-win, otherwise download the official Windows release
(one time, ~330 MB — it bundles its own Chromium) and launch that.

The launched process is windowless and is killed again when the scraper
exits. Set FLARESOLVERR_URL to use an instance you manage yourself (e.g.
in Docker); then nothing is downloaded or launched.
"""

import atexit
import os
import subprocess
import sys
import threading
import time
import zipfile

try:
    from curl_cffi import requests as _http
    _session = _http.Session(impersonate="chrome")
except ImportError:  # pragma: no cover
    import requests as _http
    _session = _http.Session()
# GitHub (and others) reject requests with no User-Agent.
_session.headers.setdefault(
    "User-Agent",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
)

DEFAULT_URL = "http://localhost:8191"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Dash keeps the install dir from ever being importable as this module's name.
INSTALL_DIR = os.path.join(BASE_DIR, "flaresolverr-win")
RELEASE_API = "https://api.github.com/repos/FlareSolverr/FlareSolverr/releases/latest"

_lock = threading.Lock()
_url = None          # confirmed-working base URL
_proc = None         # process we spawned (only we may kill it)
_failed_at = 0.0     # last failed setup; cool down instead of retrying
_RETRY_COOLDOWN = 300  # every scrape re-attempting a huge download hurts


def _is_up(url):
    try:
        r = _session.get(url + "/", timeout=4)
        return r.status_code == 200 and "FlareSolverr" in r.text
    except Exception:  # noqa: BLE001
        return False


def _find_exe():
    if not os.path.isdir(INSTALL_DIR):
        return None
    for root, _dirs, files in os.walk(INSTALL_DIR):
        if "flaresolverr.exe" in files:
            return os.path.join(root, "flaresolverr.exe")
    return None


def _download(report):
    r = _session.get(RELEASE_API, timeout=30)
    r.raise_for_status()
    release = r.json()
    asset = next(a for a in release.get("assets", [])
                 if "windows_x64" in a["name"])
    total = asset.get("size") or 0
    report(f"Downloading FlareSolverr {release.get('tag_name', '')} "
           f"(one-time, {total / 1e6:.0f} MB)…")

    os.makedirs(INSTALL_DIR, exist_ok=True)
    zip_path = os.path.join(INSTALL_DIR, "flaresolverr.zip")
    got, last_pct = 0, -10
    resp = _session.get(asset["browser_download_url"], stream=True, timeout=1800)
    try:
        resp.raise_for_status()
        with open(zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk)
                    got += len(chunk)
                    pct = int(100 * got / total) if total else 0
                    if pct >= last_pct + 10:
                        last_pct = pct
                        report(f"Downloading FlareSolverr… {pct}%")
    finally:
        resp.close()

    report("Unpacking FlareSolverr…")
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(INSTALL_DIR)
    os.remove(zip_path)


def _kill_tree():
    if _proc is not None and _proc.poll() is None:
        # taskkill /T takes the bundled Chrome down with it.
        subprocess.call(
            ["taskkill", "/F", "/T", "/PID", str(_proc.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )


def _kill_stale():
    """Clear leftover instances from an earlier run.

    Only called when nothing healthy answered on the port, so anything
    still named flaresolverr.exe is a zombie squatting the port or a
    browser profile — a fresh launch next to it just hangs.
    """
    subprocess.call(
        ["taskkill", "/F", "/IM", "flaresolverr.exe", "/T"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)


def _launch(exe, report):
    global _proc
    report("Starting FlareSolverr…")
    _kill_stale()
    # FlareSolverr configures itself from HOST/PORT env vars — the same PORT
    # this app uses for Flask. Pin them or it inherits Flask's port and
    # never shows up on 8191. Localhost-only: nothing else should reach it.
    env = dict(os.environ, HOST="127.0.0.1", PORT="8191")
    _proc = subprocess.Popen(
        [exe],
        cwd=os.path.dirname(exe),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    atexit.register(_kill_tree)
    # First boot can be slow (it sets up its bundled browser).
    deadline = time.time() + 120
    while time.time() < deadline:
        if _is_up(DEFAULT_URL):
            return True
        if _proc.poll() is not None:
            report("FlareSolverr exited unexpectedly.")
            return False
        time.sleep(1.5)
    report("FlareSolverr didn't come up in time.")
    _kill_tree()
    return False


def ensure_running(report=lambda msg: None):
    """Return a working FlareSolverr base URL, or None if unavailable.

    Blocks while another thread is already setting it up, so concurrent
    scrapes wait for one install/launch instead of racing it.
    """
    global _url, _failed_at
    with _lock:
        if _url and _is_up(_url):
            return _url

        env = os.environ.get("FLARESOLVERR_URL", "").rstrip("/")
        for candidate in ([env] if env else []) + [DEFAULT_URL]:
            if _is_up(candidate):
                _url = candidate
                return _url
        if env:
            # User pointed at their own instance and it's down —
            # not ours to install or launch anything.
            report(f"FlareSolverr at {env} is not responding.")
            return None
        if time.time() - _failed_at < _RETRY_COOLDOWN:
            return None
        if not sys.platform.startswith("win"):
            report("Auto-install of FlareSolverr is Windows-only; run it "
                   "yourself (e.g. Docker) and set FLARESOLVERR_URL.")
            _failed_at = time.time()
            return None

        exe = _find_exe()
        if exe is None:
            try:
                _download(report)
            except Exception as e:  # noqa: BLE001
                report(f"FlareSolverr download failed: {e}")
                _failed_at = time.time()
                return None
            exe = _find_exe()
            if exe is None:
                report("FlareSolverr download didn't contain flaresolverr.exe.")
                _failed_at = time.time()
                return None

        for _attempt in range(2):  # a stale-port flake deserves one retry
            if _launch(exe, report):
                _url = DEFAULT_URL
                return _url
        _failed_at = time.time()
        return None


def solve(host, report=lambda msg: None):
    """Solve the Cloudflare challenge for https://<host>/.

    Returns (cookies, user_agent) — cookies as a list of {name, value,
    domain} dicts — or (None, None) if it couldn't be solved.
    """
    base = ensure_running(report)
    if not base:
        return None, None
    report(f"Solving Cloudflare challenge for {host}…")
    try:
        r = _session.post(
            f"{base}/v1",
            json={"cmd": "request.get", "url": f"https://{host}/",
                  "maxTimeout": 60000},
            timeout=90,
        )
        sol = (r.json() or {}).get("solution") or {}
        if not sol.get("cookies"):
            return None, None
        return sol["cookies"], sol.get("userAgent")
    except Exception:  # noqa: BLE001 - solver hiccup: caller falls back
        return None, None
