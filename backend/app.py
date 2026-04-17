from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import re
from bs4 import BeautifulSoup

app = Flask(__name__)
CORS(app)

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

def extract_media(url):
    r = requests.get(url, headers=HEADERS, timeout=15)
    r.raise_for_status()
    html = r.text

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title else "Pinterest Download"

    video = re.search(r'contentUrl\":\"(https:[^"]+\.mp4[^"]*)', html)
    if video:
        return {
            "success": True,
            "type": "video",
            "title": title,
            "media": video.group(1).replace("\\u002F", "/")
        }

    image = re.search(r'<meta property=\"og:image\" content=\"([^\"]+)\"', html)
    if image:
        return {
            "success": True,
            "type": "image",
            "title": title,
            "media": image.group(1)
        }

    return {"success": False, "message": "Media not found"}

@app.route("/")
def home():
    return {"status": "ok"}

@app.route("/api/download", methods=["POST"])
def download():
    data = request.get_json()
    url = data.get("url", "")

    if not url:
        return jsonify({"success": False, "message": "No URL"}), 400

    try:
        return jsonify(extract_media(url))
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

if __name__ == "__main__":
    app.run()