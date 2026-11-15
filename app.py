import os, re, glob, shutil, tempfile, subprocess
from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import yt_dlp
from yt_dlp.utils import download_range_func

app = Flask(__name__)
CORS(app)

SECRET_COOKIES = "/etc/secrets/cookies.txt"

def to_seconds(v):
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    p = [float(x) for x in s.split(":")]
    if len(p) == 2:
        return p[0] * 60 + p[1]
    if len(p) == 3:
        return p[0] * 3600 + p[1] * 60 + p[2]
    raise ValueError("Format de temps invalide")

@app.route("/")
def home():
    return jsonify({
        "service": "ClipViral backend",
        "status": "online",
        "cookies": os.path.exists(SECRET_COOKIES),
    })

@app.route("/api/clip", methods=["POST"])
def clip():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    try:
        start = to_seconds(data.get("start_time"))
        end = to_seconds(data.get("end_time"))
    except Exception:
        return jsonify({"error": "Temps de début ou de fin invalide."}), 400
    if not url:
        return jsonify({"error": "Lien YouTube manquant."}), 400
    if end <= start:
        return jsonify({"error": "La fin doit être après le début."}), 400
    if end - start > 180:
        return jsonify({"error": "Clip trop long (3 minutes maximum)."}), 400

    tmp = tempfile.mkdtemp(prefix="clipviral_")

    @after_this_request
    def cleanup(response):
        shutil.rmtree(tmp, ignore_errors=True)
        return response

    opts = {
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "outtmpl": os.path.join(tmp, "raw.%(ext)s"),
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
    }

    if os.path.exists(SECRET_COOKIES):
        local_cookies = os.path.join(tmp, "cookies.txt")
        shutil.copy(SECRET_COOKIES, local_cookies)
        opts["cookiefile"] = local_cookies

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except Exception as e:
        return jsonify({"error": "Téléchargement impossible : " + str(e)[:300]}), 502

    files = glob.glob(os.path.join(tmp, "raw.*"))
    if not files:
        return jsonify({"error": "Aucun fichier téléchargé."}), 500

    out = os.path.join(tmp, "clip.mp4")
    cmd = [
        "ffmpeg", "-y", "-i", files[0],
        "-vf", "crop=ih*9/16:ih,scale=720:1280",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart", out,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        return jsonify({"error": "Échec du recadrage : " + r.stderr[-300:]}), 500

    return send_file(out, mimetype="video/mp4", as_attachment=True, download_name="clip.mp4")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
