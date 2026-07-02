"""Core 4chan thread scraping logic.

4chan exposes a clean read-only JSON API:
  - Thread JSON: https://a.4cdn.org/<board>/thread/<id>.json
  - Media files:  https://i.4cdn.org/<board>/<tim><ext>

This module parses a thread URL, pulls the post list, and downloads every
attached image / gif / webm, skipping files already on disk.
"""

import html
import os
import re
import time
import uuid
from datetime import datetime

import requests

# 4chan asks API clients not to hammer the API; one request per thread is plenty.
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; 4chan-thread-scraper/1.0)"
}

API_BASE = "https://a.4cdn.org"
MEDIA_BASE = "https://i.4cdn.org"

# Where media gets saved. Overridable via env var.
DOWNLOAD_ROOT = os.environ.get(
    "DOWNLOAD_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads"),
)

# Matches the board + thread id out of any common 4chan thread URL, e.g.
#   https://boards.4chan.org/gif/thread/12345678
#   https://boards.4channel.org/mu/thread/12345678/some-slug
#   4chan.org/trash/thread/12345678
# The host is required so random non-4chan links get a clear error.
_THREAD_RE = re.compile(
    r"4chan(?:nel)?\.org/([a-z0-9]+)/thread/(\d+)", re.IGNORECASE
)


class ScrapeError(Exception):
    pass


def parse_thread_url(url):
    """Return (board, thread_id) from a 4chan thread URL or raise ScrapeError."""
    m = _THREAD_RE.search(url.strip())
    if not m:
        raise ScrapeError(
            "Couldn't find a board/thread in that link. Expected something like "
            "https://boards.4chan.org/gif/thread/12345678"
        )
    return m.group(1).lower(), m.group(2)


def _sanitize(name):
    """Make an original filename safe to write to disk."""
    name = re.sub(r"[^\w.\- ]", "_", name).strip()
    return name[:120] or "file"


def _thread_dirname(thread_id, posts):
    """Folder name for a thread: '<id> - <created date> - <title>'.

    Date is when the thread was started (the OP post's timestamp). Title is
    the OP subject, falling back to 4chan's URL slug. Kept short and stripped
    of trailing dots/spaces, which Windows folder names can't end in.
    """
    date = title = ""
    if posts:
        op = posts[0]
        if op.get("time"):
            date = datetime.fromtimestamp(op["time"]).strftime("%Y-%m-%d")
        title = op.get("sub") or op.get("semantic_url") or ""
        if title:
            title = _sanitize(html.unescape(title))[:60].rstrip("._- ")
    return " - ".join(p for p in (thread_id, date, title) if p)


def fetch_thread(board, thread_id):
    """Fetch the thread JSON; return the list of posts."""
    url = f"{API_BASE}/{board}/thread/{thread_id}.json"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    if resp.status_code == 404:
        raise ScrapeError(
            f"Thread not found (404). It may have 404'd off the board, "
            f"or the board '{board}' is wrong."
        )
    resp.raise_for_status()
    return resp.json().get("posts", [])


def media_items(board, posts):
    """Yield (download_url, output_filename, size_bytes) for each attached file."""
    for post in posts:
        tim = post.get("tim")
        ext = post.get("ext")
        if not tim or not ext:
            continue  # text-only post
        original = _sanitize(post.get("filename", str(tim)))
        # Prefix the original name for readability, keep tim to guarantee uniqueness.
        out_name = f"{original}_{tim}{ext}"
        url = f"{MEDIA_BASE}/{board}/{tim}{ext}"
        yield url, out_name, post.get("fsize", 0)


def scrape(url, progress=None):
    """Scrape a thread. `progress` is an optional callable(dict) for status updates.

    Returns a summary dict.
    """
    board, thread_id = parse_thread_url(url)

    def report(**kw):
        if progress:
            progress(kw)

    report(state="fetching", message=f"Fetching /{board}/ thread {thread_id}…")
    posts = fetch_thread(board, thread_id)

    dirname = _thread_dirname(thread_id, posts)
    dest_dir = os.path.join(DOWNLOAD_ROOT, board, dirname)
    # Folders from earlier naming schemes ('<id>' or '<id> - <title>') get
    # renamed forward so their files are still seen and skipped on re-scrape.
    board_dir = os.path.join(DOWNLOAD_ROOT, board)
    if os.path.isdir(board_dir) and not os.path.exists(dest_dir):
        for entry in os.listdir(board_dir):
            if entry != dirname and (
                entry == thread_id or entry.startswith(f"{thread_id} - ")
            ):
                old_dir = os.path.join(board_dir, entry)
                if os.path.isdir(old_dir):
                    os.rename(old_dir, dest_dir)
                    break
    os.makedirs(dest_dir, exist_ok=True)

    items = list(media_items(board, posts))

    total = len(items)
    downloaded = skipped = failed = 0
    report(state="running", total=total, downloaded=0, skipped=0, failed=0,
            board=board, thread_id=thread_id, dest=dest_dir,
            message=f"Found {total} media files.")

    for i, (file_url, out_name, size) in enumerate(items, 1):
        out_path = os.path.join(dest_dir, out_name)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            skipped += 1
        else:
            try:
                _download(file_url, out_path, expected_size=size)
                downloaded += 1
            except Exception as e:  # noqa: BLE001 - keep going on individual failures
                failed += 1
                report(state="running", total=total, downloaded=downloaded,
                       skipped=skipped, failed=failed, current=out_name,
                       message=f"Failed: {out_name} ({e})")
                continue
            finally:
                # 4chan asks API clients for at most ~1 request/second.
                time.sleep(1.0)
        report(state="running", total=total, downloaded=downloaded,
               skipped=skipped, failed=failed, current=out_name,
               message=f"[{i}/{total}] {out_name}")

    summary = dict(
        state="done", total=total, downloaded=downloaded, skipped=skipped,
        failed=failed, board=board, thread_id=thread_id, dest=dest_dir,
        message=f"Done. {downloaded} downloaded, {skipped} already had, "
                f"{failed} failed.",
    )
    report(**summary)
    return summary


def _download(url, out_path, expected_size=0):
    """Stream a file to disk, writing to a temp name then renaming on success.

    The temp name is unique per call so concurrent scrapes of the same thread
    can't interleave writes, and it's always removed on failure.
    """
    tmp = f"{out_path}.{uuid.uuid4().hex[:8]}.part"
    try:
        with requests.get(url, headers=HEADERS, stream=True, timeout=60) as r:
            r.raise_for_status()
            written = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
        if expected_size and written != expected_size:
            raise ScrapeError(
                f"incomplete download: got {written} bytes, expected {expected_size}"
            )
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
