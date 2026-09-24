from flask import Flask, request, jsonify
import yt_dlp

app = Flask(__name__)


@app.route("/get-link")
def get_link():
    url = request.args.get("url")
    if not url:
        return jsonify({"error": "Missing URL"}), 400

    ydl_opts = {
        "format": "best",  # flag: selects the highest quality video/audio format available
        "skip_download": True,  # flag: skips downloading the actual file to disk (crucial for Render's ephemeral storage!)
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return jsonify({"title": info.get("title"), "stream_url": info.get("url")})


if __name__ == "__main__":
    app.run()
