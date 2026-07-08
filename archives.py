"""Thread sources: live 4chan plus third-party archive sites.

4chan threads 404 quickly; archivers keep them (usually with full media)
around for years. Nearly every big archive runs one of two engines:

  - FoolFuuka (archived.moe, desuarchive, 4plebs, arch.b4k, palanq.win,
    archiveofsins, thebarchive, ...) — clean JSON API:
      https://<host>/_/api/chan/thread/?board=<board>&num=<thread>
  - Fuuka (warosu.org) — plain HTML, which we parse.

Every source is normalized into one thread dict:

    {"source": "4chan" | host, "board": ..., "thread_id": ...,
     "date": "YYYY-MM-DD", "title": ...,
     "items": [{"post": post_no, "name": out_filename, "size": bytes or 0,
                "urls": [(download_url, referer_or_None), ...]}]}

The fallback story, since ensuring media actually downloads is the point:

  * A 4chan link that has 404'd is looked up in every archive covering
    that board (thread ids are global 4chan post numbers, so the same
    thread has the same id on every archive).
  * An archive that is down or Cloudflare-walled falls back to mirror
    archives covering the same board.
  * Each file carries a chain of candidate URLs — the archive's own copy,
    its "remote" link, the original i.4cdn.org URL — tried in order.
  * archived.moe / thebarchive / archiveofsins sit behind a Cloudflare JS
    challenge plain HTTP can't solve. We detect it, fetch clearance cookies
    through an auto-managed FlareSolverr (see flaresolverr.py) and retry.
"""

import html as _html
import os
import re
import threading
import time
from collections import namedtuple
from datetime import datetime, timezone
from urllib.parse import urlsplit

import flaresolverr

# curl_cffi impersonates a real browser's TLS fingerprint, which several
# archives (e.g. 4plebs) require. Plain requests still works for the rest,
# so it's a soft dependency.
try:
    from curl_cffi import requests as _http
    _SESSION_KW = {"impersonate": "chrome"}
except ImportError:  # pragma: no cover - degraded but functional
    import requests as _http
    _SESSION_KW = {}

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 4chan's own API gets the polite self-identifying UA it asks for.
FOURCHAN_UA = "Mozilla/5.0 (compatible; 4chan-thread-scraper/1.0)"

# Progress messages from deep inside the transport (e.g. "Downloading
# FlareSolverr… 40%") surface through a per-thread reporter, set by the
# entry points below, so each web job sees only its own messages.
_progress = threading.local()


def _report(msg):
    cb = getattr(_progress, "cb", None)
    if cb:
        cb(msg)


class SourceError(Exception):
    """Base error; scraper.ScrapeError is an alias of this."""


class ThreadNotFound(SourceError):
    pass


class CloudflareBlocked(SourceError):
    pass


ThreadRef = namedtuple("ThreadRef", "host board thread_id")  # host None = 4chan

# Known archives in fallback-preference order: freely reachable ones first,
# Cloudflare-challenged ones after, the archived.moe catch-all (boards=None
# means "has at least an entry for every board") last. Board lists are
# best-effort and only affect fallback ordering — a stale entry just costs
# one failed request.
ARCHIVES = [
    {"host": "desuarchive.org", "engine": "foolfuuka",
     "boards": set("a aco an c cgl co d fit g his int k m mlp mu q qa r9k tg trash vt wsg".split())},
    {"host": "archive.4plebs.org", "engine": "foolfuuka",
     "boards": set("adv f hr o pol s4s sp tg trv tv x".split())},
    {"host": "arch.b4k.dev", "engine": "foolfuuka",
     "boards": set("g mu v vg vm vmg vp vr vrpg vst w".split())},
    {"host": "archive.palanq.win", "engine": "foolfuuka",
     "boards": set("bant c con e i n news out p pw qst sunday toy vip vp vt w wg wsr".split())},
    {"host": "warosu.org", "engine": "fuuka",
     "boards": set("3 biz cgl ck cm diy fa ic jp lit sci vr vt".split())},
    {"host": "archiveofsins.com", "engine": "foolfuuka",
     "boards": set("h hc hm i lgbt r s soc t u".split())},
    {"host": "thebarchive.com", "engine": "foolfuuka",
     "boards": set("b bant".split())},
    {"host": "archived.moe", "engine": "foolfuuka", "boards": None},
]

# Alternate hostnames people paste for the same archive.
HOST_ALIASES = {
    "4plebs.org": "archive.4plebs.org",
    "arch.b4k.co": "arch.b4k.dev",
    "b4k.co": "arch.b4k.dev",
    "b4k.dev": "arch.b4k.dev",
    "palanq.win": "archive.palanq.win",
}

_ENGINES = {a["host"]: a["engine"] for a in ARCHIVES}

# Matches board + thread id (+ host) from 4chan or any archive thread URL:
#   https://boards.4chan.org/gif/thread/12345678/slug
#   https://archived.moe/gif/thread/25135045/#q25135045
#   warosu.org/jp/thread/S10000000   (warosu's S prefix = full-size images)
_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?P<host>[a-z0-9][a-z0-9.\-]*\.[a-z]{2,})"
    r"/(?P<board>[a-z0-9]+)/thread/S?(?P<num>\d+)",
    re.IGNORECASE,
)


def parse_thread_url(url):
    """Return a ThreadRef from a 4chan or archive thread URL, or raise."""
    m = _URL_RE.search(url.strip())
    if not m:
        raise SourceError(
            "Couldn't find a board/thread in that link. Expected a 4chan or "
            "archive URL like https://boards.4chan.org/gif/thread/12345678 "
            "or https://archived.moe/gif/thread/12345678"
        )
    host = m.group("host").lower()
    board = m.group("board").lower()
    num = m.group("num")
    if re.search(r"4chan(?:nel)?\.org$|4cdn\.org$", host):
        return ThreadRef(None, board, num)
    return ThreadRef(HOST_ALIASES.get(host, host), board, num)


# ---------------------------------------------------------------------------
# HTTP transport: one browser-impersonating session per host, with Cloudflare
# challenge detection and optional FlareSolverr clearance.

_sessions = {}
_sessions_lock = threading.Lock()
# Cloudflare re-challenges a session now and then even with valid clearance
# cookies, so allow a few re-solves per host — but cap it, since each solve
# is a slow headless-browser round-trip.
_clearance_attempts = {}
_MAX_CLEARANCE_ATTEMPTS = 3


def _session(host):
    with _sessions_lock:
        s = _sessions.get(host)
        if s is None:
            s = _http.Session(**_SESSION_KW)
            if not _SESSION_KW:
                s.headers["User-Agent"] = BROWSER_UA
            _sessions[host] = s
        return s


def _looks_like_cf_challenge(resp, headers_only=False):
    if resp.status_code not in (403, 503):
        return False
    if resp.headers.get("cf-mitigated") == "challenge":
        return True
    if headers_only:  # streamed body — don't consume it just to sniff
        return False
    ctype = resp.headers.get("content-type") or ""
    if "html" not in ctype:
        return False
    head = resp.text[:2000]
    return "Just a moment" in head or "_cf_chl" in head or "challenge-platform" in head


def _flaresolverr_clearance(host, session):
    """Ask FlareSolverr to pass the challenge; copy its cookies + UA over.

    cf_clearance is tied to the solving IP and User-Agent, so we adopt the
    solver's UA for this session.
    """
    attempts = _clearance_attempts.get(host, 0)
    if attempts >= _MAX_CLEARANCE_ATTEMPTS:
        return False
    _clearance_attempts[host] = attempts + 1
    cookies, user_agent = flaresolverr.solve(host, report=_report)
    if not cookies:
        return False
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain") or host)
    if user_agent:
        session.headers["User-Agent"] = user_agent
    return True


def _is_4chan_host(host):
    return bool(re.search(r"(?:^|\.)(?:4chan|4channel|4cdn)\.org$", host or ""))


def _get(url, referer=None, timeout=30, stream=False):
    host = urlsplit(url if "://" in url else f"https://{url}").hostname
    s = _session(host)
    headers = {"Referer": referer} if referer else {}
    r = s.get(url, headers=headers, timeout=timeout, stream=stream)
    # Solve-and-retry until clear; _flaresolverr_clearance caps the attempts.
    # 4chan's own hosts are never worth a solver round-trip: live files don't
    # challenge, so a challenge there just means the file is gone.
    while _looks_like_cf_challenge(r, headers_only=stream):
        if stream:
            r.close()
        if _is_4chan_host(host) or not _flaresolverr_clearance(host, s):
            raise CloudflareBlocked(
                f"{host} is behind a Cloudflare challenge this scraper "
                f"can't solve"
            )
        r = s.get(url, headers=headers, timeout=timeout, stream=stream)
    return r


def open_stream(url, referer=None):
    """Streamed GET for media downloads, routed through the host's session."""
    return _get(url, referer=referer, timeout=60, stream=True)


# ---------------------------------------------------------------------------
# Normalization helpers

def sanitize(name):
    """Make an original filename / title safe to write to disk."""
    name = re.sub(r"[^\w.\- ]", "_", name).strip()
    return name[:120] or "file"


def _clean_title(text):
    if not text:
        return ""
    return sanitize(_html.unescape(text))[:60].rstrip("._- ")


def _title_from_comment(comment):
    """First few words of the OP text, like 4chan's own URL slugs."""
    if not comment:
        return ""
    return _clean_title(" ".join(comment.split())[:60])


def _https(link):
    """Archives hand out a mix of http/https links to the same hosts;
    normalize so URL-chain dedup works."""
    if link and link.startswith("http://"):
        return "https://" + link[len("http://"):]
    return link


# ---------------------------------------------------------------------------
# Source: live 4chan (JSON API)

def fetch_4chan(board, thread_id):
    url = f"https://a.4cdn.org/{board}/thread/{thread_id}.json"
    s = _session("a.4cdn.org")
    r = s.get(url, headers={"User-Agent": FOURCHAN_UA}, timeout=30)
    if r.status_code == 404:
        raise ThreadNotFound(f"/{board}/{thread_id} is not (or no longer) on 4chan")
    r.raise_for_status()
    posts = r.json().get("posts", [])

    date = title = ""
    if posts:
        op = posts[0]
        if op.get("time"):
            # Local time, matching how this scraper has always named folders.
            date = datetime.fromtimestamp(op["time"]).strftime("%Y-%m-%d")
        title = _clean_title(op.get("sub") or op.get("semantic_url") or "")

    items = []
    for post in posts:
        tim, ext = post.get("tim"), post.get("ext")
        if not tim or not ext:
            continue  # text-only post
        original = sanitize(post.get("filename", str(tim)))
        items.append({
            "post": str(post.get("no", tim)),
            "name": f"{original}_{tim}{ext}",
            "size": post.get("fsize", 0),
            "urls": [(f"https://i.4cdn.org/{board}/{tim}{ext}", None)],
            "thumb": (f"https://i.4cdn.org/{board}/{tim}s.jpg", None),
        })
    return {"source": "4chan", "board": board, "thread_id": thread_id,
            "date": date, "title": title, "items": items}


# ---------------------------------------------------------------------------
# Source: FoolFuuka archives (JSON API)

def fetch_foolfuuka(host, board, thread_id):
    url = f"https://{host}/_/api/chan/thread/?board={board}&num={thread_id}"
    r = _get(url, referer=f"https://{host}/{board}/")
    if r.status_code == 404:
        raise ThreadNotFound(f"thread not on {host}")
    if "json" not in (r.headers.get("content-type") or ""):
        raise SourceError(f"{host} didn't return JSON (HTTP {r.status_code})")
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise ThreadNotFound(f"{host}: {data['error']}")
    thread = (data or {}).get(str(thread_id))
    if not thread:
        raise ThreadNotFound(f"{host} returned no thread data")

    op = thread.get("op") or {}
    replies = thread.get("posts") or {}
    posts = [op] + [replies[k] for k in sorted(replies, key=lambda k: int(k))]

    date = ""
    if op.get("timestamp"):
        # Asagi-based archives store timestamps shifted to America/New_York;
        # formatting as UTC reproduces the date the archive itself displays.
        date = datetime.fromtimestamp(
            int(op["timestamp"]), tz=timezone.utc
        ).strftime("%Y-%m-%d")
    title = _clean_title(op.get("title") or "") or _title_from_comment(op.get("comment"))

    page = f"https://{host}/{board}/thread/{thread_id}/"
    items = []
    for post in posts:
        media = post.get("media")
        if not media or not media.get("media_orig"):
            continue
        tim_ext = media["media_orig"]  # e.g. "1425671487004.webm"
        stem, ext = os.path.splitext(tim_ext)
        orig = sanitize(os.path.splitext(media.get("media_filename") or "")[0] or stem)
        # Chain of places this exact file might still exist, best first.
        urls = []
        for link in (media.get("media_link"), media.get("remote_media_link")):
            link = _https(link)
            if link and (link, page) not in urls:
                urls.append((link, page))
        native = (f"https://i.4cdn.org/{board}/{tim_ext}", None)
        if native not in urls:
            urls.append(native)
        thumb = _https(media.get("thumb_link"))
        items.append({
            "post": str(post.get("num", stem)),
            "name": f"{orig}_{stem}{ext}",
            "size": int(media.get("media_size") or 0),
            "urls": urls,
            "thumb": (thumb, page) if thumb else None,
        })
    return {"source": host, "board": board, "thread_id": thread_id,
            "date": date, "title": title, "items": items}


# ---------------------------------------------------------------------------
# Source: Fuuka archives (warosu) — no API, parse the thread HTML.

_FUUKA_FILEINFO_RE = re.compile(
    r'class="fileinfo[^"]*">\s*File:\s*(?P<info>[^<]*?)\s*</span>', re.S)
_FUUKA_SUBJECT_RE = re.compile(r'class="filetitle"[^>]*>(.*?)<', re.S)
_FUUKA_TIME_RE = re.compile(r'class="posttime"[^>]*>\s*([^<]+?)\s*<', re.S)


def fetch_fuuka(host, board, thread_id):
    page = f"https://{host}/{board}/thread/{thread_id}"
    r = _get(page)
    if r.status_code == 404:
        raise ThreadNotFound(f"thread not on {host}")
    if r.status_code != 200:
        raise SourceError(f"{host} returned HTTP {r.status_code}")
    text = r.text

    m = _FUUKA_SUBJECT_RE.search(text)
    title = _clean_title(m.group(1)) if m else ""
    date = ""
    m = _FUUKA_TIME_RE.search(text)
    if m:
        try:  # e.g. "Tue, Dec 5, 2013 23:45:49"
            date = datetime.strptime(
                m.group(1).strip(), "%a, %b %d, %Y %H:%M:%S"
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass

    items = []
    infos = list(_FUUKA_FILEINFO_RE.finditer(text))
    img_re = re.compile(
        r'href="(?P<url>https?://[^"]+/data/%s/img/[^"]+)"' % re.escape(board))
    for i, m in enumerate(infos):
        # Full-size image link lives between this fileinfo span and the next.
        chunk_end = infos[i + 1].start() if i + 1 < len(infos) else len(text)
        link = img_re.search(text, m.end(), chunk_end)
        if not link:
            continue
        url = link.group("url")
        tim_ext = url.rsplit("/", 1)[-1]  # "1352177112402.jpg"
        stem, ext = os.path.splitext(tim_ext)
        # fileinfo text: "26 KB, 350x256, original name.jpg"
        parts = [p.strip() for p in _html.unescape(m.group("info")).split(",", 2)]
        orig = sanitize(os.path.splitext(parts[2])[0]) if len(parts) == 3 else stem
        # Host part excludes '/' so this can't latch onto a warosu URL
        # embedded inside an iqdb/saucenao search link.
        thumb = re.search(
            r'https?://[^/"\s]+/data/%s/thumb/[^"\s]+' % re.escape(board),
            text[m.end():chunk_end])
        items.append({
            "post": stem,
            "name": f"{orig}_{stem}{ext}",
            "size": 0,  # Fuuka only shows rounded sizes; don't enforce
            "urls": [(url, page),
                     (f"https://i.4cdn.org/{board}/{tim_ext}", None)],
            "thumb": (thumb.group(0), page) if thumb else None,
        })
    return {"source": host, "board": board, "thread_id": thread_id,
            "date": date, "title": title, "items": items}


# ---------------------------------------------------------------------------
# Dispatch + fallback across archives

def fetch_archive(host, board, thread_id):
    """Fetch a thread from one archive host (unknown hosts: assume FoolFuuka,
    which is what virtually every modern archive runs)."""
    host = HOST_ALIASES.get(host, host)
    if _ENGINES.get(host) == "fuuka":
        return fetch_fuuka(host, board, thread_id)
    return fetch_foolfuuka(host, board, thread_id)


def _archives_covering(board, exclude=()):
    return [a["host"] for a in ARCHIVES
            if a["host"] not in exclude
            and (a["boards"] is None or board in a["boards"])]


def _fetch_from_archives(board, thread_id, exclude, report, first_error=None):
    errors = [first_error] if first_error else []
    for host in _archives_covering(board, exclude):
        report(f"Checking {host}…")
        try:
            thread = fetch_archive(host, board, thread_id)
            report(f"Found thread on {host}.")
            return thread
        except SourceError as e:
            errors.append(str(e))
        except Exception as e:  # noqa: BLE001 - dead host, bad TLS, parked domain…
            errors.append(f"{host}: {type(e).__name__}")
        time.sleep(0.5)  # be gentle when walking multiple archives
    msg = (f"Couldn't fetch /{board}/{thread_id} from any archive "
           f"({'; '.join(errors) or 'no archive covers this board'}).")
    if any("Cloudflare" in e for e in errors):
        msg += (" The automatic Cloudflare solver (FlareSolverr) wasn't "
                "available — check the server console, then retry.")
    raise ThreadNotFound(msg)


def load_thread(ref, report=lambda msg: None):
    """Fetch a normalized thread for a ThreadRef, falling back across sources."""
    _progress.cb = report
    board, thread_id = ref.board, ref.thread_id
    if ref.host is None:
        try:
            return fetch_4chan(board, thread_id)
        except ThreadNotFound:
            report("Thread is gone from 4chan — searching the archives…")
            return _fetch_from_archives(board, thread_id, (), report)
    try:
        return fetch_archive(ref.host, board, thread_id)
    except SourceError as e:
        report(f"{ref.host} failed — trying mirror archives…")
        return _fetch_from_archives(
            board, thread_id, {ref.host}, report, first_error=str(e))


def mirror_media_links(board, thread_id, exclude=(), report=lambda msg: None,
                       max_mirrors=2):
    """Collect {post_no: [(url, referer), ...]} from other sources.

    Used when a file's whole URL chain failed: live 4chan (if the thread is
    still up — archives constructed from truncated pre-microsecond tims can
    miss it) or another archive may still hold a copy under the same global
    post number.
    """
    _progress.cb = report
    links, hits = {}, 0
    sources = ([] if "4chan" in exclude else ["4chan"]) \
        + _archives_covering(board, exclude)
    for host in sources:
        report(f"Looking for missing files on {host}…")
        try:
            if host == "4chan":
                thread = fetch_4chan(board, thread_id)
            else:
                thread = fetch_archive(host, board, thread_id)
        except Exception:  # noqa: BLE001 - mirrors are best-effort
            continue
        for item in thread["items"]:
            bucket = links.setdefault(item["post"], [])
            bucket.extend(u for u in item["urls"] if u not in bucket)
        hits += 1
        if hits >= max_mirrors:
            break
        time.sleep(0.5)
    return links
