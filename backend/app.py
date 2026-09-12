from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
import requests, re, json
from bs4 import BeautifulSoup
from urllib.parse import quote

app = Flask(__name__)
CORS(app, expose_headers=["Content-Range", "Accept-Ranges",
                          "Content-Length", "Content-Disposition"])

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

DIRECT_MEDIA_RE = re.compile(r"\.(jpe?g|png|gif|webp|mp4)(\?|$)", re.I)
VIDEO_EXT_RE    = re.compile(r"\.mp4(\?|$)", re.I)
CHUNK = 64 * 1024          # 64 KB — keeps memory per-request tiny


# ============================================================
#  EXTRACTION (same as before)
# ============================================================
def _clean(u):
    if not u: return u
    return u.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")

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
        for i in node: _find_media(i, found)

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
        # Optional proxy URL. Only use this if you NEED a custom filename
        # or need to force CORS. Otherwise just use result["media"] directly
        # (client downloads from Pinterest — 0 bytes through your server).
        result["stream_url"] = (
            f"/api/stream?url={quote(result['media'], safe='')}"
        )
        return jsonify(result)
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "message": f"Request failed: {e}"}), 502
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/api/stream", methods=["GET", "HEAD"])
def stream():
    """
    Zero-storage, resumable proxy.

    * Forwards the client's `Range` header straight to Pinterest.
    * Returns Pinterest's `206 Partial Content` + `Content-Range` verbatim.
    * Uses no disk and no in-memory cache.
    * Chrome/IDM/aria2/wget -c can pause for days and resume — because
      resume is negotiated between the client and Pinterest, not us.
    """
    target = request.args.get("url", "").strip()
    if not target:
        return jsonify({"success": False, "message": "No URL"}), 400

    # If a pin page URL was passed, resolve to direct media first.
    if re.search(r"(pinterest\.[a-z.]+/pin/|pin\.it/)", target, re.I):
        r = extract_media(target)
        if not r.get("success"):
            return jsonify({"success": False, "message": r.get("message")}), 400
        target = r["media"]

    # Forward Range (the whole point of this endpoint).
    fwd = {"Accept-Encoding": "identity"}
    if request.headers.get("Range"):
        fwd["Range"] = request.headers["Range"]

    try:
        upstream = SESSION.get(target, headers=fwd, stream=True,
                               timeout=(10, 60), allow_redirects=True)
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "message": str(e)}), 502

    # Pass through status (200 or 206) and key headers.
    headers = {
        "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400",
    }
    for h in ("Content-Length", "Content-Range"):
        if h in upstream.headers:
            headers[h] = upstream.headers[h]

    is_video = VIDEO_EXT_RE.search(target) or \
               upstream.headers.get("Content-Type", "").startswith("video/")
    ext = "mp4" if is_video else "jpg"
    headers["Content-Disposition"] = f'attachment; filename="pinterest_media.{ext}"'

    if request.method == "HEAD":
        upstream.close()
        return Response(status=upstream.status_code, headers=headers)

    def gen():
        try:
            for chunk in upstream.iter_content(chunk_size=CHUNK):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    return Response(stream_with_context(gen()),
                    status=upstream.status_code, headers=headers)


if __name__ == "__main__":
    app.run(threaded=True)