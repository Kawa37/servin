import os
import re
import requests
import yt_dlp
from flask import Flask, request, Response, jsonify
from flask_cors import CORS  # added: browser blocks cross-origin fetch without this

app = Flask(__name__)
CORS(app)  # allows your Flutter web app (any localhost port) to call this API


def sanitize_filename(name: str) -> str:
    # Strip characters that break Content-Disposition headers / filesystems
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:150]


@app.route('/download')
def download_video():
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "Missing url parameter"}), 400

    ydl_opts = {
        'format': 'best[ext=mp4]/best',  # prefer a single mp4 with audio+video already merged
        'skip_download': True,
        'quiet': True,
        'noplaylist': True,
        # Added: cloud IPs (Render, AWS, etc.) get YouTube's bot-check far more
        # than home IPs. Requesting the android player client often avoids it.
        'extractor_args': {'youtube': {'player_client': ['android']}},
    }

    # Wrapped in try/except: original code crashed with a raw 500 on age-gated,
    # private, or geo-blocked videos instead of returning a clean error
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)}), 400

    stream_url = info.get('url')
    if not stream_url:
        return jsonify({"error": "No downloadable stream found for this URL"}), 422

    filename = sanitize_filename(info.get('title', 'video')) + ".mp4"

    # Added: some CDNs (YouTube's included) reject requests with no User-Agent
    # or that are missing headers yt-dlp already knows are required
    upstream_headers = {'User-Agent': 'Mozilla/5.0'}
    if info.get('http_headers'):
        upstream_headers.update(info['http_headers'])

    upstream = requests.get(stream_url, headers=upstream_headers, stream=True, timeout=30)
    if upstream.status_code != 200:
        return jsonify({"error": f"Upstream fetch failed: {upstream.status_code}"}), 502

    def generate():
        # Generator instead of returning req.iter_content directly: lets us
        # guarantee upstream.close() runs even if the client disconnects early
        try:
            for chunk in upstream.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    resp_headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
    }
    # Added: forward Content-Length so the Flutter client can show a real % progress bar
    content_length = upstream.headers.get('content-length')
    if content_length:
        resp_headers["Content-Length"] = content_length

    return Response(
        generate(),
        content_type=upstream.headers.get('content-type', 'video/mp4'),
        headers=resp_headers,
        direct_passthrough=True,  # tells Werkzeug not to buffer the whole generator in memory
    )


if __name__ == '__main__':
    # Render sets $PORT itself; gunicorn (see Procfile) reads it in production
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
