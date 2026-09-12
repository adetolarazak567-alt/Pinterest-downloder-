from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import requests, re, json, os, time, hashlib, threading, tempfile, unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from urllib.parse import quote

app = Flask(__name__)
CORS(app, expose_headers=["Content-Range", "Accept-Ranges",
                          "Content-Length", "Content-Disposition"])

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "identity",   # critical: prevents gzip/length mismatch
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

DIRECT_MEDIA_RE = re.compile(r"\.(jpe?g|png|gif|webp|mp4)(\?|$)", re.I)
IMAGE_EXT_RE    = re.compile(r"\.(jpe?g|png|gif|webp)(\?|$)", re.I)
VIDEO_EXT_RE    = re.compile(r"\.mp4(\?|$)", re.I)

CACHE_DIR = os.environ.get("PIN_CACHE_DIR",
                           os.path.join(tempfile.gettempdir(), "pin_cache"))
os.makedirs(CACHE_DIR, exist_ok=True)

JOBS, JOBS_LOCK = {}, threading.Lock()


# ============================================================
#  MEDIA EXTRACTION  (unchanged behaviour, cleaner code)
# ============================================================
def _clean(u):
    if not u: return u
    return (u.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&"))

def _upgrade_image(url):
    url = _clean(url)
    if not url: return url
    return re.sub(r"(https?://i\.pinimg\.com/)(?:\d+x\d*|originals)/",
                  r"\1originals/", url)

def _area(d):
    try: return int(d.get("width") or 0) * int(d.get("height") or 0)
    except Exception: return 0

def _find_media(node, found):
    if isinstance(node, dict):
        v = node.get("videos")
        if isinstance(v, dict) and isinstance(v.get("video_list"), dict):
            for item in v["video_list"].values():
                if not isinstance(item, dict): continue
                u = _clean(item.get("url") or "")
                if not u.lower().endswith(".mp4"): continue
                if _area(item) >= _area(found.get("video", {})):
                    found["video"] = {"url": u, "width": item.get("width"),
                                      "height": item.get("height")}
        imgs = node.get("images")
        if isinstance(imgs, dict):
            for key in ("orig", "originals"):
                img = imgs.get(key)
                if isinstance(img, dict) and img.get("url"):
                    if _area(img) >= _area(found.get("image", {})):
                        found["image"] = {"url": _upgrade_image(img["url"]),
                                          "width": img.get("width"),
                                          "height": img.get("height")}
        for val in node.values(): _find_media(val, found)
    elif isinstance(node, list):
        for item in node: _find_media(item, found)

def extract_media(url):
    if DIRECT_MEDIA_RE.search(url):
        if VIDEO_EXT_RE.search(url):
            return {"success": True, "type": "video",
                    "title": "Pinterest Video", "media": url}
        return {"success": True, "type": "image",
                "title": "Pinterest Image", "media": _upgrade_image(url)}

    r = SESSION.get(url, timeout=20, allow_redirects=True)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or "utf-8"
    html = r.text

    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string.strip() if (soup.title and soup.title.string)
             else "Pinterest Download")
    found = {}

    script = soup.find("script", id="__PWS_DATA__")
    if script and script.string:
        try: _find_media(json.loads(script.string), found)
        except Exception: pass

    if "video" not in found:
        m = re.search(r'"(?:contentUrl|url)"\s*:\s*"(https:[^"]+?\.mp4[^"]*)"', html)
        if m: found["video"] = {"url": _clean(m.group(1))}

    if "image" not in found:
        m = (re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', html))
        if m: found["image"] = {"url": _upgrade_image(m.group(1))}

    if "image" not in found:
        m = re.search(r'https://i\.pinimg\.com/[^"\\\s]+?\.(?:jpe?g|png|webp)', html, re.I)
        if m: found["image"] = {"url": _upgrade_image(m.group(0))}

    if "video" in found:
        return {"success": True, "type": "video", "title": title,
                "media": found["video"]["url"],
                "width": found["video"].get("width"),
                "height": found["video"].get("height"),
                "thumbnail": found.get("image", {}).get("url")}
    if "image" in found:
        return {"success": True, "type": "image", "title": title,
                "media": found["image"]["url"],
                "width": found["image"].get("width"),
                "height": found["image"].get("height")}
    return {"success": False, "message": "Media not found"}


# ============================================================
#  PARALLEL RESUMMABLE DOWNLOADER
# ============================================================
class DownloadJob:
    """Downloads a remote URL in parallel chunks to a local cache file
    while streaming bytes to any number of concurrent clients."""

    def __init__(self, url, workers=8, chunk_bytes=4 * 1024 * 1024):
        self.url = url
        self.key = hashlib.sha256(url.encode()).hexdigest()
        self.path = os.path.join(CACHE_DIR, self.key + ".part")
        self.done_marker = self.path + ".done"
        self.workers = workers
        self.chunk_bytes = chunk_bytes

        self.total_size = None
        self.final_url = None
        self.accepts_ranges = False
        self.content_type = "application/octet-stream"
        self.progress = 0
        self.complete = False
        self.failed = False
        self.error = None
        self.created_at = time.time()

        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)
        self._started = False

    # ---------- lifecycle ----------
    def start(self):
        # A previous run finished → serve instantly.
        if os.path.exists(self.done_marker) and os.path.exists(self.path):
            self.total_size = os.path.getsize(self.path)
            self.progress = self.total_size
            self.complete = True
            return
        if self._started: return
        self._started = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self._probe()
            if not self.accepts_ranges or self.total_size < 4 * 1024 * 1024:
                self._download_sequential()
            else:
                self._download_parallel()
            with open(self.done_marker, "w") as f:
                f.write(str(self.total_size))
            with self.cond:
                self.progress = self.total_size
                self.complete = True
                self.cond.notify_all()
        except Exception as e:
            with self.cond:
                self.failed = True
                self.error = str(e)
                self.cond.notify_all()

    # ---------- metadata ----------
    def _probe(self):
        try:
            r = SESSION.head(self.url, allow_redirects=True, timeout=20)
            r.raise_for_status()
            self.final_url = r.url
            self.total_size = int(r.headers.get("Content-Length", 0))
            self.accepts_ranges = "bytes" in r.headers.get("Accept-Ranges", "").lower()
            self.content_type = r.headers.get("Content-Type", self.content_type)
        except Exception:
            r = SESSION.get(self.url, headers={"Range": "bytes=0-0"},
                            allow_redirects=True, timeout=20, stream=True)
            r.raise_for_status()
            self.final_url = r.url
            self.content_type = r.headers.get("Content-Type", self.content_type)
            m = re.search(r"/(\d+)", r.headers.get("Content-Range", ""))
            if m:
                self.total_size = int(m.group(1))
                self.accepts_ranges = True
            r.close()
        if not self.final_url:
            self.final_url = self.url

    # ---------- strategies ----------
    def _download_sequential(self):
        r = SESSION.get(self.final_url, stream=True, timeout=(15, 60))
        r.raise_for_status()
        cl = r.headers.get("Content-Length")
        if cl: self.total_size = int(cl)
        with open(self.path, "wb") as f:
            for c in r.iter_content(chunk_size=256 * 1024):
                if not c: continue
                f.write(c)
                with self.cond:
                    self.progress += len(c)
                    self.cond.notify_all()

    def _download_parallel(self):
        # Pre-allocate the file so multiple threads can seek & write.
        with open(self.path, "wb") as f:
            f.truncate(self.total_size)

        n_chunks = max(self.workers * 4, 16)
        size = (self.total_size + n_chunks - 1) // n_chunks
        chunks, pos = [], 0
        while pos < self.total_size:
            end = min(pos + size - 1, self.total_size - 1)
            chunks.append((pos, end))
            pos = end + 1

        completed = []

        def fetch(start, end):
            last_err = None
            for attempt in range(4):
                try:
                    h = {"Range": f"bytes={start}-{end}"}
                    r = SESSION.get(self.final_url, headers=h, stream=True,
                                    timeout=(15, 60))
                    if r.status_code == 200:
                        # Origin ignored Range → read whole thing, slice.
                        r.close()
                        r = SESSION.get(self.final_url, stream=True, timeout=(15, 60))
                        with open(self.path, "r+b") as f:
                            f.seek(start)
                            cur = 0
                            for c in r.iter_content(chunk_size=64 * 1024):
                                if not c: continue
                                c_end = cur + len(c) - 1
                                if cur > end: break
                                if c_end < start:
                                    cur = c_end + 1; continue
                                lo = max(0, start - cur)
                                hi = min(len(c), end - cur + 1)
                                f.write(c[lo:hi])
                                cur = c_end + 1
                    elif r.status_code == 206:
                        with open(self.path, "r+b") as f:
                            f.seek(start)
                            for c in r.iter_content(chunk_size=64 * 1024):
                                if c: f.write(c)
                    else:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    break
                except Exception as e:
                    last_err = e
                    time.sleep(1.5 * (attempt + 1))
            else:
                raise last_err or RuntimeError("chunk failed")

            with self.cond:
                completed.append((start, end))
                completed.sort()
                prog = 0
                for s, e in completed:
                    if s <= prog: prog = max(prog, e + 1)
                    else: break
                self.progress = prog
                self.cond.notify_all()

        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            futs = [ex.submit(fetch, s, e) for s, e in chunks]
            for f in as_completed(futs):
                f.result()

    # ---------- client-facing ----------
    def wait_ready(self, timeout=45):
        deadline = time.time() + timeout
        with self.cond:
            while self.total_size is None and not self.failed:
                remaining = deadline - time.time()
                if remaining <= 0: return False
                self.cond.wait(timeout=remaining)
        return self.total_size is not None

    def _wait_offset(self, offset):
        with self.cond:
            while not self.complete and not self.failed and self.progress <= offset:
                self.cond.wait(timeout=1.0)
            return not self.failed

    def serve(self, start, end):
        if not self._wait_offset(start): return
        pos = start
        with open(self.path, "rb") as f:
            while pos <= end:
                if not self._wait_offset(pos): return
                with self.cond:
                    avail_end = min(self.progress - 1, end)
                if avail_end < pos:
                    time.sleep(0.05); continue
                to_read = min(512 * 1024, avail_end - pos + 1)
                f.seek(pos)
                data = f.read(to_read)
                if not data:
                    time.sleep(0.05); continue
                pos += len(data)
                yield data


# ============================================================
#  JOB MANAGER
# ============================================================
def _get_job(media_url):
    with JOBS_LOCK:
        job = JOBS.get(media_url)
        if job is None:
            job = DownloadJob(media_url)
            JOBS[media_url] = job
            job.start()
        return job


def _sanitize_filename(name):
    name = unicodedata.normalize("NFKD", name or "")
    name = re.sub(r"[^\w\s.-]", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return (name or "pinterest_media")[:80]


def _resolve_media_url(url):
    """If it's a Pinterest page URL, resolve to the direct media URL."""
    if re.search(r"(pinterest\.[a-z.]+/pin/|pin\.it/)", url, re.I):
        r = extract_media(url)
        if not r.get("success"):
            raise ValueError(r.get("message", "Media not found"))
        return r["media"], r.get("type", "image")
    return url, ("video" if VIDEO_EXT_RE.search(url) else "image")


# ============================================================
#  ROUTES
# ============================================================
@app.route("/")
def home():
    return {"status": "ok"}


@app.route("/api/download", methods=["POST"])
def download():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"success": False, "message": "No URL"}), 400
    try:
        result = extract_media(url)
        if not result.get("success"):
            return jsonify(result), 404
        # Add a stable, resumable streaming URL the client can hand to Chrome.
        result["stream_url"] = (
            f"/api/stream?url={quote(result['media'], safe='')}"
            f"&filename={quote(_sanitize_filename(result.get('title')), safe='')}"
        )
        return jsonify(result)
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "message": f"Request failed: {e}"}), 502
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/stream", methods=["GET", "HEAD"])
def stream():
    """
    Resumable, parallel-download proxy.

    * Accepts either a direct media URL or a Pinterest pin page URL.
    * Honours `Range: bytes=X-Y` requests (returns 206 with Content-Range).
    * Downloads the file in parallel 4 MB chunks server-side; streams bytes
      to the client as soon as they are available.
    * `Accept-Ranges: bytes` + persistent cache allow Chrome to pause and
      resume days later without restarting.
    """
    media_url = request.args.get("url", "").strip()
    if not media_url:
        return jsonify({"success": False, "message": "No URL"}), 400

    try:
        media_url, media_type = _resolve_media_url(media_url)
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 400

    job = _get_job(media_url)

    if not job.wait_ready(timeout=45):
        return jsonify({"success": False, "message": job.error or "Probe timeout"}), 504
    if job.failed:
        return jsonify({"success": False, "message": job.error or "Download failed"}), 502

    # ----- parse Range -----
    rng = request.headers.get("Range")
    start, end = 0, job.total_size - 1
    status = 200
    if rng:
        m = re.match(r"bytes=(\d*)-(\d*)", rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1): start = int(m.group(1))
            if m.group(2): end = int(m.group(2))
            end = min(end, job.total_size - 1)
            if start > end or start >= job.total_size:
                return Response(
                    status=416,
                    headers={"Content-Range": f"bytes */{job.total_size}"})
            status = 206

    length = end - start + 1
    fname = _sanitize_filename(request.args.get("filename", "pinterest_media"))
    ext = "mp4" if media_type == "video" else "jpg"

    resp_headers = {
        "Content-Type": job.content_type or "application/octet-stream",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{fname}.{ext}"',
        "Cache-Control": "public, max-age=86400",
    }
    if status == 206:
        resp_headers["Content-Range"] = f"bytes {start}-{end}/{job.total_size}"

    # HEAD: return headers only.
    if request.method == "HEAD":
        return Response(status=status, headers=resp_headers)

    return Response(
        stream_with_context(job.serve(start, end)),
        status=status, headers=resp_headers)


if __name__ == "__main__":
    app.run(threaded=True)