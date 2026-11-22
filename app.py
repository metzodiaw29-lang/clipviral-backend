import io
import os
import subprocess
import tempfile
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRET_COOKIES_PATH = "/etc/secrets/cookies.txt"
LOCAL_COOKIES_PATH = os.path.join(BASE_DIR, "cookies.txt")
COOKIES_PATH = SECRET_COOKIES_PATH if os.path.exists(SECRET_COOKIES_PATH) else LOCAL_COOKIES_PATH
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


def copy_cookies_to_tmpdir(tmpdir):
    source_path = None
    if os.path.exists(SECRET_COOKIES_PATH):
        source_path = SECRET_COOKIES_PATH
    elif os.path.exists(LOCAL_COOKIES_PATH):
        source_path = LOCAL_COOKIES_PATH
    elif COOKIES_PATH and os.path.exists(COOKIES_PATH):
        source_path = COOKIES_PATH

    if not source_path:
        return None

    temp_cookies_path = os.path.join(tmpdir, "cookies.txt")
    with open(source_path, "r", encoding="utf-8", errors="ignore") as src_file:
        content = src_file.read()
    with open(temp_cookies_path, "w", encoding="utf-8") as dst_file:
        dst_file.write(content)
    return temp_cookies_path


def escape_drawtext(text):
    value = str(text or "")
    value = value.replace("\\", "\\\\")
    value = value.replace(":", "\\:")
    value = value.replace("'", "’")
    value = value.replace("%", "\\%")
    value = value.replace("\r", " ").replace("\n", " ")
    return value.strip()


def find_downloaded_file(tmpdir, prefix):
    for name in os.listdir(tmpdir):
        if name.startswith(prefix) and not name.endswith(".part") and not name.endswith(".ytdl"):
            full_path = os.path.join(tmpdir, name)
            if os.path.isfile(full_path) and os.path.getsize(full_path) > 0:
                return full_path
    return None


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200


@app.route("/api/info", methods=["GET", "OPTIONS"])
def video_info():
    if request.method == "OPTIONS":
        return "", 204

    url = (request.args.get("url") or "").strip()
    if not url:
        return jsonify({"error": "Paramètre url manquant"}), 400

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_cookies = copy_cookies_to_tmpdir(tmpdir)
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--no-playlist",
            "--print",
            "duration",
        ]
        if temp_cookies:
            cmd.extend(["--cookies", temp_cookies])
        cmd.append(url)

        print("yt-dlp info début", flush=True)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Récupération de la durée trop longue"}), 500
        print("yt-dlp info fini", flush=True)

        if proc.returncode != 0:
            err_msg = (proc.stderr or proc.stdout or "Erreur yt-dlp").strip()
            return jsonify({"error": err_msg}), 500

        lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
        if not lines:
            return jsonify({"error": "Durée vidéo introuvable"}), 500

        raw_duration = lines[-1]
        try:
            duration_seconds = int(round(float(raw_duration)))
        except ValueError:
            return jsonify({"error": f"Durée vidéo invalide : {raw_duration}"}), 500

        if duration_seconds <= 0:
            return jsonify({"error": "Durée vidéo nulle ou invalide"}), 500

        return jsonify({"duration": duration_seconds}), 200


@app.route("/api/clip", methods=["POST", "OPTIONS"])
def clip_video():
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL vidéo manquante"}), 400

    try:
        start_time = float(data.get("start_time", 0))
        end_time = float(data.get("end_time", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "start_time ou end_time invalide"}), 400

    if end_time <= start_time:
        return jsonify({"error": "end_time doit être supérieur à start_time"}), 400

    clip_format = (data.get("format") or data.get("crop_mode") or "zoom").strip().lower()
    if clip_format not in ("zoom", "crop", "bars"):
        clip_format = "zoom"

    subtitles = data.get("subtitles") or []
    if not isinstance(subtitles, list):
        subtitles = []

    try:
        sub_offset = float(data.get("sub_offset", 0) or 0)
    except (TypeError, ValueError):
        sub_offset = 0.0

    clip_duration = end_time - start_time
    dl_start = max(0, int(start_time) - 2)
    dl_end = int(end_time) + 2

    with tempfile.TemporaryDirectory() as tmpdir:
        temp_cookies = copy_cookies_to_tmpdir(tmpdir)
        section_template = os.path.join(tmpdir, "section.%(ext)s")
        yt_format = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best"

        section_cmd = [
            "yt-dlp",
            "--no-playlist",
            "--download-sections",
            f"*{dl_start}-{dl_end}",
            "-f",
            yt_format,
            "--merge-output-format",
            "mp4",
            "-o",
            section_template,
        ]
        if temp_cookies:
            section_cmd.extend(["--cookies", temp_cookies])
        section_cmd.append(url)

        print("yt-dlp début", flush=True)
        try:
            sec_proc = subprocess.run(section_cmd, capture_output=True, text=True, timeout=200)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Téléchargement trop long"}), 500

        input_path = None
        used_fallback = False

        if sec_proc.returncode == 0:
            input_path = find_downloaded_file(tmpdir, "section.")

        if not input_path:
            used_fallback = True
            temp_cookies_fallback = copy_cookies_to_tmpdir(tmpdir)
            full_template = os.path.join(tmpdir, "full.%(ext)s")
            full_cmd = [
                "yt-dlp",
                "--no-playlist",
                "-f",
                yt_format,
                "--merge-output-format",
                "mp4",
                "-o",
                full_template,
            ]
            if temp_cookies_fallback:
                full_cmd.extend(["--cookies", temp_cookies_fallback])
            full_cmd.append(url)

            try:
                full_proc = subprocess.run(full_cmd, capture_output=True, text=True, timeout=200)
            except subprocess.TimeoutExpired:
                return jsonify({"error": "Téléchargement trop long"}), 500

            if full_proc.returncode != 0:
                err_msg = (full_proc.stderr or full_proc.stdout or sec_proc.stderr or "Erreur yt-dlp").strip()
                return jsonify({"error": err_msg}), 500

            input_path = find_downloaded_file(tmpdir, "full.")
            if not input_path:
                return jsonify({"error": "Fichier vidéo introuvable après téléchargement"}), 500

        print("yt-dlp fini", flush=True)

        ss_time = start_time if used_fallback else max(0.0, start_time - dl_start)

        if clip_format == "zoom":
            vf_parts = ["scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280"]
        elif clip_format == "crop":
            vf_parts = ["crop=min(iw\\,ih*9/16):ih,scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280"]
        else:
            vf_parts = ["scale=720:1280:force_original_aspect_ratio=decrease,pad=720:1280:(ow-iw)/2:(oh-ih)/2:black"]

        valid_subs = []
        for sub in subtitles:
            if not isinstance(sub, dict):
                continue
            txt = escape_drawtext(sub.get("text", ""))
            if not txt:
                continue
            try:
                s_t = max(0.0, float(sub.get("start", 0)) + sub_offset)
                e_t = max(s_t + 0.1, float(sub.get("end", s_t + 1.5)) + sub_offset)
            except (TypeError, ValueError):
                continue
            valid_subs.append((s_t, e_t, txt))

        has_subtitles = len(valid_subs) > 0
        if has_subtitles:
            font_arg = f"fontfile={FONT_PATH}:" if os.path.exists(FONT_PATH) else ""
            for s_t, e_t, txt in valid_subs:
                vf_parts.append(
                    f"drawtext={font_arg}text='{txt}':fontcolor=white:fontsize=38:"
                    f"borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-220:"
                    f"enable='between(t,{s_t:.2f},{e_t:.2f})'"
                )

        vf_filter = ",".join(vf_parts)
        output_path = os.path.join(tmpdir, "output.mp4")

        fps_value = "24" if has_subtitles else "30"
        crf_value = "30" if has_subtitles else "28"

        ffmpeg_cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            f"{ss_time:.2f}",
            "-i",
            input_path,
            "-t",
            f"{clip_duration:.2f}",
            "-vf",
            vf_filter,
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            crf_value,
            "-r",
            fps_value,
            "-threads",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            output_path,
        ]

        print("ffmpeg début", flush=True)
        try:
            ff_proc = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=360)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "Découpe trop longue"}), 500
        print("ffmpeg fini", flush=True)

        if ff_proc.returncode != 0 or not os.path.exists(output_path):
            err_msg = (ff_proc.stderr or ff_proc.stdout or "Erreur ffmpeg").strip()
            return jsonify({"error": err_msg}), 500

        with open(output_path, "rb") as out_f:
            video_bytes = io.BytesIO(out_f.read())
        video_bytes.seek(0)

        return send_file(
            video_bytes,
            mimetype="video/mp4",
            as_attachment=True,
            download_name="clip.mp4",
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
