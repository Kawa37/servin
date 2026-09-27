import os
import re
import requests
import yt_dlp
from urllib.parse import quote  # Added: for RFC 5987 percent-encoding of the filename
from flask import Flask, request, Response, jsonify
from flask_cors import CORS  # Added: needed so browser fetch() calls aren't blocked

app = Flask(__name__)

# Added: allow any origin to call this API, and explicitly expose the two
# headers the web client reads (Content-Length for progress, Content-Disposition
# for filename). Without expose_headers, fetch() can enable CORS and still get
# null back from response.headers.get('Content-Length').
CORS(app, resources={r"/download": {"origins": "*"}},
     expose_headers=["Content-Length", "Content-Disposition"])


def sanitize_filename(name: str) -> str:
    # Strip characters that break filesystems, then strip control/newline
    # characters that would make the raw string an invalid HTTP header value
    # (this is what was crashing on captions used as titles, e.g. some
    # Facebook posts with no real title, which can contain emoji, line
    # breaks, and other non-ASCII text).
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r"[\r\n\t]", " ", name)
    return name.strip()[:150]


@app.route("/download")
def download_video():
    url = request.args.get("url")
    res = request.args.get("res")
    if not url:
        return jsonify({"error": "Missing url parameter"}), 400

    ydl_opts = {
        "format": "best[ext=mp4]/best",  # prefer a single mp4 with audio+video already merged
        "skip_download": True,
        "quiet": True,
        "noplaylist": True,
    }

    # Wrapped in try/except: original code crashed with a raw 500 on age-gated,
    # private, or geo-blocked videos instead of returning a clean error
    try:
        with yt_dlp.YoutubeDL(
            ydl_opts,
        ) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)}), 400

    stream_url = info.get("url")
    if not stream_url:
        return jsonify({"error": "No downloadable stream found for this URL"}), 422

    filename = sanitize_filename(info.get("title", "video")) + ".mp4"

    # Added: some CDNs (YouTube's included) reject requests with no User-Agent
    # or that are missing headers yt-dlp already knows are required
    upstream_headers = {"User-Agent": "Mozilla/5.0"}
    if info.get("http_headers"):
        upstream_headers.update(info["http_headers"])

    upstream = requests.get(
        stream_url, headers=upstream_headers, stream=True, timeout=30
    )
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

    # Added: ASCII-only fallback for clients/proxies that don't understand
    # filename*=, plus a properly percent-encoded UTF-8 version for everyone
    # else. Neither of these can contain raw emoji/Unicode in the header
    # itself -- that's what was causing the "Invalid HTTP Header" 400s.
    ascii_fallback = filename.encode("ascii", "ignore").decode() or "video.mp4"
    encoded_filename = quote(filename)
    resp_headers = {
        "Content-Disposition": (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{encoded_filename}"
        ),
    }
    # Added: forward Content-Length so the client can show a real % progress bar
    content_length = upstream.headers.get("content-length")
    if content_length:
        resp_headers["Content-Length"] = content_length

    return Response(
        generate(),
        content_type=upstream.headers.get("content-type", "video/mp4"),
        headers=resp_headers,
        direct_passthrough=True,  # tells Werkzeug not to buffer the whole generator in memory
    )


if __name__ == "__main__":
    # Render sets $PORT itself; gunicorn (see Procfile) reads it in production
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
