# 4chan Thread Scraper

Paste a 4chan thread link, get every pic, gif, and webm downloaded to your PC.
Runs as a small web app you can open from your phone over Wi-Fi.

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

## Notes

- Works on any board: /gif/, /hr/, /trash/, /mu/, etc.
- Re-running the same link only grabs **new** files — already-downloaded media
  is skipped, so you can re-scrape an active thread for new replies.
- Uses 4chan's public JSON API; nothing is sent anywhere except to 4chan.
- If your phone can't reach the PC, allow Python through the Windows Firewall
  for Private networks (Windows usually prompts the first time you run it).
