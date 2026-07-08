"""Core thread scraping logic: fetch a thread (live 4chan or an archive
site — see archives.py), then download every attached image / gif / webm,
skipping files already on disk.

Folders are named '<thread id> - <date started> - <title>' regardless of
which source the thread came from, so a thread grabbed live and re-grabbed
later from an archive lands in the same place.
"""

import os
import re
import time
import uuid

import archives

# app.py and older callers catch scraper.ScrapeError; keep that name working.
ScrapeError = archives.SourceError
parse_thread_url = archives.parse_thread_url

# Where media gets saved. Overridable via env var.
DOWNLOAD_ROOT = os.environ.get(
    "DOWNLOAD_ROOT",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads"),
)


def _thread_dirname(thread):
    """'<id> - <created date> - <title>', dropping any missing part.

    Stripped of trailing dots/spaces, which Windows folder names can't end in.
    """
    return " - ".join(
        p for p in (thread["thread_id"], thread["date"], thread["title"]) if p
    ).rstrip("._- ")


def scrape(url, progress=None):
    """Scrape a thread. `progress` is an optional callable(dict) for status updates.

    Returns a summary dict.
    """
    ref = parse_thread_url(url)
    board, thread_id = ref.board, ref.thread_id

    def report(**kw):
        if progress:
            progress(kw)

    source_label = ref.host or "4chan"
    report(state="fetching",
           message=f"Fetching /{board}/ thread {thread_id} from {source_label}…")
    thread = archives.load_thread(
        ref, report=lambda msg: report(state="fetching", message=msg))
    source = thread["source"]
    via = "" if source == "4chan" else f" (via {source})"

    dirname = _thread_dirname(thread)
    dest_dir = os.path.join(DOWNLOAD_ROOT, board, dirname)
    # Folders from earlier naming schemes ('<id>' or '<id> - …') get renamed
    # forward so their files are still seen and skipped on re-scrape.
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

    items = thread["items"]
    total = len(items)
    downloaded = skipped = failed = thumbs = 0
    report(state="running", total=total, downloaded=0, skipped=0, failed=0,
           thumbs=0, board=board, thread_id=thread_id, dest=dest_dir,
           message=f"Found {total} media files{via}.")

    # Filled lazily the first time a file's whole URL chain fails: other
    # archives may still hold a copy under the same global post number.
    mirror_links = None

    existing = set(os.listdir(dest_dir))
    for i, item in enumerate(items, 1):
        out_name = item["name"]
        out_path = os.path.join(dest_dir, out_name)
        note = ""
        if _already_have(out_name, out_path, existing):
            skipped += 1
        else:
            err = _download_any(item["urls"], out_path)
            if err is not None:
                if mirror_links is None:
                    mirror_links = archives.mirror_media_links(
                        board, thread_id, exclude={source},
                        report=lambda msg: report(
                            state="running", total=total, downloaded=downloaded,
                            skipped=skipped, failed=failed, thumbs=thumbs,
                            message=msg))
                extra = [u for u in mirror_links.get(item["post"], [])
                         if u not in item["urls"]]
                if extra:
                    err = _download_any(extra, out_path)
            if err is None:
                downloaded += 1
                existing.add(out_name)
            elif _save_thumb(item, dest_dir):
                # Full file is gone from every source; its thumbnail is all
                # that's left of it. Saved under thumbs\, counted separately.
                thumbs += 1
                note = " — full file lost, saved thumbnail"
            else:
                failed += 1
                report(state="running", total=total, downloaded=downloaded,
                       skipped=skipped, failed=failed, thumbs=thumbs,
                       current=out_name, message=f"Failed: {out_name} ({err})")
                time.sleep(0.5)  # don't hammer hosts with rapid-fire failures
                continue
            # Archives (and 4chan) ask for at most ~1 request/second.
            time.sleep(1.0)
        report(state="running", total=total, downloaded=downloaded,
               skipped=skipped, failed=failed, thumbs=thumbs, current=out_name,
               message=f"[{i}/{total}] {out_name}{note}")

    message = (f"Done{via}. {downloaded} downloaded, {skipped} already had, "
               f"{failed} failed.")
    if thumbs:
        message += (f" {thumbs} files no longer exist in full anywhere — "
                    f"their thumbnails are in \\thumbs.")
    summary = dict(
        state="done", total=total, downloaded=downloaded, skipped=skipped,
        failed=failed, thumbs=thumbs, board=board, thread_id=thread_id,
        dest=dest_dir, message=message,
    )
    report(**summary)
    return summary


def _already_have(out_name, out_path, existing):
    """True if this file (or a same-tim variant of it) is already on disk.

    4chan tims grew from millisecond (13-digit) to microsecond (16-digit)
    precision, and Asagi-based archives store the truncated 13-digit form —
    so the same file scraped live vs. from an archive can differ in name.
    Match on the first 13 digits of the tim to dedupe across sources.
    """
    if out_name in existing and os.path.getsize(out_path) > 0:
        return True
    stem, ext = os.path.splitext(out_name)
    m = re.match(r"(.*_\d{13})\d*$", stem)
    if m:
        prefix = m.group(1)
        return any(f.startswith(prefix) and f.endswith(ext) for f in existing)
    return False


def _save_thumb(item, dest_dir):
    """Last resort: save the archive's thumbnail under the thumbs subfolder.

    Returns True if the thumbnail is (now) on disk. Kept in a subfolder with
    its own name so it can never be mistaken for, or shadow, the real file.
    """
    if not item.get("thumb"):
        return False
    thumb_url = item["thumb"][0]
    ext = os.path.splitext(thumb_url.rsplit("/", 1)[-1])[1] or ".jpg"
    path = os.path.join(dest_dir, "thumbs",
                        os.path.splitext(item["name"])[0] + ext)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return True
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return _download_any([item["thumb"]], path) is None


def _download_any(urls, out_path):
    """Try each (url, referer) in order; return None on success, else the
    last error."""
    last_err = None
    for url, referer in urls:
        try:
            _download(url, referer, out_path)
            return None
        except Exception as e:  # noqa: BLE001 - fall through to the next URL
            last_err = e
    return last_err or ScrapeError("no download URL for this file")


def _download(url, referer, out_path):
    """Stream a file to disk, writing to a temp name then renaming on success.

    The temp name is unique per call so concurrent scrapes of the same thread
    can't interleave writes, and it's always removed on failure.
    """
    tmp = f"{out_path}.{uuid.uuid4().hex[:8]}.part"
    try:
        r = archives.open_stream(url, referer=referer)
        try:
            if r.status_code != 200:
                raise ScrapeError(f"HTTP {r.status_code} from {url}")
            ctype = r.headers.get("content-type") or ""
            if "text/html" in ctype:
                # An error/challenge page, not media — never save it as a file.
                raise ScrapeError(f"got an HTML page instead of media from {url}")
            # 4chan's original byte count is deliberately not enforced here —
            # archives sometimes serve a re-encoded copy. The server's own
            # Content-Length is what detects a truncated transfer.
            promised = 0
            if not r.headers.get("content-encoding"):
                promised = int(r.headers.get("content-length") or 0)
            written = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
                        written += len(chunk)
        finally:
            r.close()
        if written == 0:
            raise ScrapeError(f"empty response from {url}")
        if promised and written != promised:
            raise ScrapeError(
                f"incomplete download: got {written} bytes, expected {promised}"
            )
        os.replace(tmp, out_path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
