# 4chan Thread Scraper

Paste a 4chan **or archive-site** thread link, get every pic, gif, and webm
downloaded to your PC. Runs as a small web app you can open from your phone
over Wi-Fi.

## Setup (one time)

```
pip install -r requirements.txt
```

## Run

```
python app.py
```

It prints two URLs:

- **On this PC:** http://localhost:5000
- **On your phone:** http://<your-PC-IP>:5000  (must be on the same Wi-Fi)

Open that on your phone, paste a thread link (e.g.
`https://boards.4chan.org/gif/thread/12345678`), tap **Scrape**.

## Where files go

`downloads/<board>/<thread_id> - <date started> - <thread title>/` next to
`app.py`, e.g. `downloads/gif/12345678 - 2026-07-01 - Cool Webms/`. Threads
with no subject line drop the title part. Set a different location with the
`DOWNLOAD_ROOT` environment variable.

## Archive sites (for threads that already 404'd)

4chan threads are ephemeral, so the scraper also understands the common
archivers. Paste an archive thread link exactly like a 4chan one, e.g.
`https://archived.moe/gif/thread/25135045/#q25135045` or
`https://desuarchive.org/wsg/thread/12345678`. Folders get the same
`<id> - <date> - <title>` labels, so a thread grabbed live and re-grabbed
later from an archive lands in the same place.

Supported: **archived.moe, desuarchive.org, 4plebs, arch.b4k.dev,
archive.palanq.win, archiveofsins.com, thebarchive.com** (all FoolFuuka-based)
and **warosu.org** (Fuuka-based). Unknown archive hosts are attempted with the
standard FoolFuuka API too, so most others just work.

Built-in fallbacks, in the name of getting every file:

- A **4chan link that has 404'd** is looked up automatically in every archive
  covering that board.
- An archive that's down or blocked falls back to **mirror archives** (thread
  ids are global, so the same thread has the same id everywhere).
- Each file tries a chain of URLs: the archive's copy, its remote link, then
  the original `i.4cdn.org` file — and if all of those fail, live 4chan and
  other archives are checked for that exact file.
- When a full file no longer exists **anywhere** (common on boards whose
  full-media archives shut down, like /gif/), the archive's thumbnail is
  saved into a `thumbs\` subfolder instead — counted separately as
  "thumb-only" in the UI, never passed off as the real file.

**Cloudflare-walled archives** (archived.moe, thebarchive, archiveofsins run a
JS challenge that blocks scripts): handled automatically. The app quietly runs
[FlareSolverr](https://github.com/FlareSolverr/FlareSolverr) — a small local
service that passes the challenge in a headless browser — downloading it once
(~330 MB, into `flaresolverr-win/`) the first time the app starts. No setup
needed; it's stopped again when the app exits. If you already run your own
FlareSolverr (e.g. in Docker), set `FLARESOLVERR_URL` and nothing is
downloaded or launched.

## Notes

- Works on any board: /gif/, /hr/, /trash/, /mu/, etc.
- Re-running the same link only grabs **new** files — already-downloaded media
  is skipped, so you can re-scrape an active thread for new replies.
- Uses 4chan's public JSON API and the archives' public FoolFuuka APIs;
  nothing is sent anywhere else.
- If your phone can't reach the PC, allow Python through the Windows Firewall
  for Private networks (Windows usually prompts the first time you run it).
