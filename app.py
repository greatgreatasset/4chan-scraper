"""Tiny Flask web UI for the 4chan thread scraper.

Run it on your PC, then from your phone (same Wi-Fi) open http://<PC-IP>:5000
Paste a thread link, tap Scrape, and media saves into ./downloads on the PC.
"""

import os
import socket
import threading
import uuid

from flask import Flask, jsonify, render_template, request

import scraper

app = Flask(__name__)

# In-memory job registry: job_id -> status dict. Fine for single-user personal use.
_jobs = {}
# (board, thread_id) -> job_id, so double-submitting a thread reuses the running job.
_active = {}
_jobs_lock = threading.Lock()

# Keep this many finished jobs around for late /status polls; prune the rest.
_MAX_FINISHED_JOBS = 20


def _set(job_id, data):
    with _jobs_lock:
        _jobs[job_id] = data


def _prune_finished():
    """Drop all but the most recent finished jobs. Call with _jobs_lock held."""
    finished = [j for j, d in _jobs.items() if d.get("state") in ("done", "error")]
    for job_id in finished[:-_MAX_FINISHED_JOBS]:
        del _jobs[job_id]


def _run_job(job_id, url, key):
    def progress(update):
        _set(job_id, update)

    try:
        scraper.scrape(url, progress=progress)
    except scraper.ScrapeError as e:
        _set(job_id, {"state": "error", "message": str(e)})
    except Exception as e:  # noqa: BLE001
        _set(job_id, {"state": "error", "message": f"Unexpected error: {e}"})
    finally:
        with _jobs_lock:
            if _active.get(key) == job_id:
                del _active[key]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/scrape", methods=["POST"])
def scrape_route():
    body = request.get_json(silent=True) or {}
    url = (request.form.get("url") or body.get("url", "")).strip()
    if not url:
        return jsonify({"error": "No URL provided."}), 400
    # Validate early so the user gets immediate feedback on a bad link.
    try:
        key = scraper.parse_thread_url(url)
    except scraper.ScrapeError as e:
        return jsonify({"error": str(e)}), 400

    with _jobs_lock:
        # If this thread is already being scraped, hand back the running job.
        existing = _active.get(key)
        if existing is not None:
            return jsonify({"job_id": existing})
        job_id = uuid.uuid4().hex
        _jobs[job_id] = {"state": "queued", "message": "Starting…"}
        _active[key] = job_id
        _prune_finished()

    t = threading.Thread(target=_run_job, args=(job_id, url, key), daemon=True)
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status_route(job_id):
    with _jobs_lock:
        data = _jobs.get(job_id)
    if data is None:
        return jsonify({"state": "unknown", "message": "No such job."}), 404
    return jsonify(data)


def _lan_ip():
    """Best-effort local IP so we can print a phone-friendly URL on startup."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:  # noqa: BLE001
        return "127.0.0.1"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    ip = _lan_ip()
    print("\n  4chan thread scraper running.")
    print(f"  On this PC:    http://localhost:{port}")
    print(f"  On your phone: http://{ip}:{port}   (same Wi-Fi)")
    print(f"  Saving to:     {scraper.DOWNLOAD_ROOT}\n")
    # host=0.0.0.0 makes it reachable from other devices on the LAN.
    app.run(host="0.0.0.0", port=port, threaded=True)
