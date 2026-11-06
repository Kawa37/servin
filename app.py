import os
import re
import shutil  # locate ffmpeg, copy cookies
import subprocess  # run ffmpeg as a child process (like child_process.spawn in Node)
import requests
import yt_dlp
from urllib.parse import quote
from flask import Flask, request, Response, jsonify
from flask_cors import CORS

app = Flask(__name__)

# CHANGED: CORS now also covers the new /info route
CORS(
    app,
    resources={r"/download": {"origins": "*"}, r"/info": {"origins": "*"}},
    expose_headers=["Content-Length", "Content-Disposition", "X-Estimated-Length"],
)

# Render Secret Files are mounted read-only at /etc/secrets, but yt-dlp
# wants to write refreshed cookies back to the file. Copy to /tmp at startup.
COOKIES_SRC = "/etc/secrets/cookies.txt"
COOKIES_PATH = "/tmp/cookies.txt"
if os.path.exists(COOKIES_SRC):
    shutil.copy(COOKIES_SRC, COOKIES_PATH)


# ADDED: health-check route so Render's "GET / 404" log lines go away
@app.route("/")
def health():
    return jsonify({"status": "ok"})


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r"[\r\n\t]", " ", name)
    return name.strip()[:150]


# ADDED: shared yt-dlp options so /info and /download can't drift apart
# (like a DEFAULT_OPTS dict in Python that both functions spread with {**opts})
def base_opts():
    opts = {
        "skip_download": True,
        "quiet": True,
        "noplaylist": True,
        # yt-dlp needs a JS runtime to solve YouTube's challenges (deno is in the Dockerfile)
        "js_runtimes": {"deno": {}},
        "remote_components": ["ejs:github"],
    }
    if os.path.exists(COOKIES_PATH):
        opts["cookiefile"] = COOKIES_PATH
    if os.environ.get("PROXY_URL"):
        opts["proxy"] = os.environ["PROXY_URL"]
    return opts


# CHANGED: added "/best" at the very end as a last resort, so a height filter
# that matches nothing no longer throws "Requested format is not available"
# "/" works like "or" in a JS fallback chain: a || b || c
def build_format(res):
    h = f"[height<={res}]" if res else ""
    return (
        f"bestvideo{h}[ext=mp4][vcodec^=avc]+bestaudio[ext=m4a]/"  # mp4-compatible, fastest to copy
        f"bestvideo{h}+bestaudio/"  # anything (vp9/av1/opus)
        f"best{h}[ext=mp4]/best{h}/"  # single-file fallback
        f"best"  # ADDED: never fail on the filter
    )


# ffmpeg wants headers as one "Key: Value\r\n..." string per input
def header_string(headers: dict) -> str:
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


# stream video+audio merged through ffmpeg stdout, never touching disk
def stream_merged(formats, container):
    cmd = ["ffmpeg", "-loglevel", "error"]
    for f in formats:
        hdrs = {"User-Agent": "Mozilla/5.0", **(f.get("http_headers") or {})}
        cmd += ["-headers", header_string(hdrs), "-i", f["url"]]
    cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c", "copy"]  # copy = no re-encode
    if container == "mp4":
        # fragmented mp4 is required because stdout isn't seekable
        cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4"]
    elif container == "webm":
        cmd += ["-f", "webm"]
    else:
        cmd += ["-f", "matroska"]
    cmd += ["pipe:1"]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(256 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            # kill ffmpeg if the client disconnects early
            proc.kill()
            proc.wait()

    return generate()


# ADDED: returns the list of real resolutions for a video
@app.route("/info")
def video_info():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "Missing url parameter"}), 400

    # ignore_no_formats_error: still return metadata even if yt-dlp can't pick a format
    opts = {**base_opts(), "ignore_no_formats_error": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)}), 400

    # set comprehension = JS: [...new Set(formats.filter(...).map(f => f.height))]
    # keep only entries that have a height and are not audio-only (vcodec == "none")
    heights = sorted(
        {
            f["height"]
            for f in info.get("formats", [])
            if f.get("height") and f.get("vcodec") != "none"
        },
        reverse=True,  # highest first
    )
    return jsonify(
        {
            "title": info.get("title", "video"),
            "duration": info.get("duration"),
            "heights": heights,
        }
    )


@app.route("/download")
def download_video():
    url = request.args.get("url")
    res = request.args.get("res")
    if not url:
        return jsonify({"error": "Missing url parameter"}), 400

    # accept "720", "720p", "1080p" etc.
    m = re.search(r"\d+", res or "")
    max_height = int(m.group()) if m else None

    # CHANGED: reuse the shared options, only the format differs per request
    # (was a big inline dict before)
    ydl_opts = {**base_opts(), "format": build_format(max_height)}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": str(e)}), 400

    title = sanitize_filename(info.get("title", "video"))
    ext = info.get("ext", "mp4")  # yt-dlp computes the merged container for us
    filename = f"{title}.{ext}"

    ascii_fallback = filename.encode("ascii", "ignore").decode() or f"video.{ext}"
    resp_headers = {
        "Content-Disposition": (
            f'attachment; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(filename)}"
        ),
    }

    # separate video+audio (typical for YouTube above 720p) -> merge via ffmpeg
    requested = info.get("requested_formats")
    if requested and len(requested) == 2:
        if not shutil.which("ffmpeg"):
            return jsonify({"error": "ffmpeg not installed on server"}), 500
        # Total size is unknown when muxing on the fly, so send an estimate instead
        est = sum(
            (f.get("filesize") or f.get("filesize_approx") or 0) for f in requested
        )
        if est:
            resp_headers["X-Estimated-Length"] = str(est)
        mime = {"mp4": "video/mp4", "webm": "video/webm"}.get(ext, "video/x-matroska")
        return Response(
            stream_merged(requested, ext),
            content_type=mime,
            headers=resp_headers,
            direct_passthrough=True,
        )

    # ---- single-file path ----
    stream_url = info.get("url")
    if not stream_url:
        return jsonify({"error": "No downloadable stream found for this URL"}), 422

    upstream_headers = {"User-Agent": "Mozilla/5.0"}
    if info.get("http_headers"):
        upstream_headers.update(info["http_headers"])

    upstream = requests.get(
        stream_url, headers=upstream_headers, stream=True, timeout=30
    )
    if upstream.status_code != 200:
        return jsonify({"error": f"Upstream fetch failed: {upstream.status_code}"}), 502

    def generate():
        try:
            for chunk in upstream.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()

    content_length = upstream.headers.get("content-length")
    if content_length:
        resp_headers["Content-Length"] = content_length

    return Response(
        generate(),
        content_type=upstream.headers.get("content-type", "video/mp4"),
        headers=resp_headers,
        direct_passthrough=True,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
