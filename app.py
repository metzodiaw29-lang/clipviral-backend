import os, re, glob, shutil, tempfile, subprocess, textwrap
from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import yt_dlp
from yt_dlp.utils import download_range_func

app = Flask(__name__)
CORS(app)

SECRET_COOKIES = "/etc/secrets/cookies.txt"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

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

def wrap_title(title):
    t = re.sub(r"\s+", " ", str(title or "")).strip()[:70]
    if not t:
        return None
    lines = textwrap.wrap(t, width=22)[:3]
    return "\n".join(lines)

@app.route("/")
def home():
    return jsonify({
        "service": "ClipViral backend",
        "status": "online",
        "cookies": os.path.exists(SECRET_COOKIES),
        "font": os.path.exists(FONT),
    })

@app.route("/api/clip", methods=["POST"])
def clip():
    data = request.get_json(silent=True) or {}
    url = data.get("url")
    mode = str(data.get("crop_mode") or "zoom").lower()
    title = wrap_title(data.get("title"))
    try:
        start = to_seconds(data.get("start_time"))
        end = to_seconds(data.get("end_time"))
    except Exception:
        return jsonify({"error": "Temps de début ou de fin invalide."}), 400
    if not url:
        return jsonify({"error": "Lien YouTube manquant."}), 400
    if end <= start:
        return jsonify({"error": "La fin doit être après le début."}), 400
    if end - start > 90:
        return jsonify({"error": "Clip trop long (90 secondes maximum)."}), 400

    tmp = tempfile.mkdtemp(prefix="clipviral_")

    @after_this_request
    def cleanup(response):
        shutil.rmtree(tmp, ignore_errors=True)
        return response

    opts = {
        "format": "bv*[height<=480]+ba/b[height<=480]/bv*+ba/b/best",
        "outtmpl": os.path.join(tmp, "raw.%(ext)s"),
        "download_ranges": download_range_func(None, [(start, end)]),
        "force_keyframes_at_cuts": False,
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "socket_timeout": 30,
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

    if mode == "crop":
        vf = ["-vf", "crop=ih*9/16:ih,scale=480:854"]
    else:
        if mode == "fit":
            fg = "[b]scale=480:-2[fg]"
        else:
            fg = "[b]crop=w='min(iw,ih*1.22)':h=ih,scale=480:394[fg]"
        graph = (
            "[0:v]split[a][b];"
            "[a]crop=w='min(iw,ih*9/16)':h=ih,scale=48:86,scale=480:854:flags=bilinear,boxblur=6[bg];"
            + fg + ";"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
        if title and os.path.exists(FONT):
            tfile = os.path.join(tmp, "title.txt")
            with open(tfile, "w", encoding="utf-8") as f:
                f.write(title)
            graph += (
                "[v];[v]drawtext=fontfile=" + FONT +
                ":textfile=" + tfile +
                ":fontsize=26:fontcolor=black:box=1:boxcolor=white@0.95"
                ":boxborderw=14:line_spacing=6:x=(w-text_w)/2:y=70"
            )
        vf = ["-filter_complex", graph]

    cmd = [
        "ffmpeg", "-y", "-i", files[0],
        *vf,
        "-r", "30",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-threads", "1",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", out,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=420)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Le recadrage a pris trop de temps."}), 504
    if r.returncode != 0 or not os.path.exists(out):
        return jsonify({"error": "Échec du recadrage : " + r.stderr[-300:]}), 500

    return send_file(out, mimetype="video/mp4", as_attachment=True, download_name="clip.mp4")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
