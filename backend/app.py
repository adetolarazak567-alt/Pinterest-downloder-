from flask import Flask, request, jsonify from flask_cors import CORS import requests, re from bs4 import BeautifulSoup

app = Flask(name) CORS(app)

HEADERS = { 'User-Agent': 'Mozilla/5.0' }

def extract_media(url): r = requests.get(url, headers=HEADERS, timeout=15) r.raise_for_status() html = r.text title = 'Pinterest Download' soup = BeautifulSoup(html, 'html.parser') if soup.title and soup.title.string: title = soup.title.string.strip()

# Try video first
m = re.search(r'"contentUrl":"(https:[^\"]+\.mp4[^"]*)"', html)
if m:
    media = m.group(1).replace('\\u002F','/').replace('\\','')
    return {'success': True, 'type': 'video', 'title': title, 'media': media}

# Try image
m = re.search(r'"image":"(https:[^\"]+)"', html)
if m:
    media = m.group(1).replace('\\u002F','/').replace('\\','')
    return {'success': True, 'type': 'image', 'title': title, 'media': media}

og = re.search(r'<meta property="og:image" content="([^"]+)"', html)
if og:
    return {'success': True, 'type': 'image', 'title': title, 'media': og.group(1)}

return {'success': False, 'message': 'Media not found'}

@app.route('/api/download', methods=['POST']) def download(): data = request.get_json(silent=True) or {} url = data.get('url','').strip() if not url: return jsonify({'success': False, 'message': 'URL required'}), 400 try: return jsonify(extract_media(url)) except Exception as e: return jsonify({'success': False, 'message': str(e)}), 500

@app.route('/') def home(): return {'status':'ok','message':'Pinterest Downloader API running'}

if name == 'main': app.run(host='0.0.0.0', port=5000, debug=True)
