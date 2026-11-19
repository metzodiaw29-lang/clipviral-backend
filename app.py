import io
import os
import subprocess
import tempfile
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

COOKIES_PATH = "/etc/secrets/cookies.txt"
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


def parse_time_seconds(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def normalize_format(data):
    raw = str(data.get("format") or data.get("crop_mode") or "zoom").strip().lower()
    if raw in ("bars", "bandes_noires", "bandes noires", "letterbox"):
        return "bars"
    if raw in ("crop", "fullscreen", "plein_ecran", "plein écran"):
        return "crop"
    return "zoom"


def format_ass_timestamp(seconds):
    sec = max(0.0, float(seconds))
    total_cs = int(round(sec * 100))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def escape_ass_text(text):
    cleaned = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    cleaned = cleaned.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return cleaned


def build_ass_file(subtitles, sub_offset, ass_path):
    valid_subs = []
    for item in subtitles or []:
        if not isinstance(item, dict):
            continue
        start = parse_time_seconds(item.get("start"))
        end = parse_time_seconds(item.get("end"))
        text = escape_ass_text(item.get("text", ""))
        if start is None or end is None or not text:
            continue
        s = max(0.0, start + float(sub_offset or 0.0))
        e = max(s + 0.2, end + float(sub_offset or 0.0))
        valid_subs.append((s, e, text))

    if not valid_subs:
        return False

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 720
PlayResY: 1280
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,36,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,2,40,40,140,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for s, e, text in valid_subs:
        lines.append(
            f"Dialogue: 0,{format_ass_timestamp(s)},{format_ass_timestamp(e)},Default,,0,0,0,,{text}\n"
        )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    return True


@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/api/clip", methods=["POST", "OPTIONS"])
def create_clip():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    url = str(data.get("url") or "").strip()
    start_time = parse_time_seconds(data.get("start_time"))
    end_time = parse_time_seconds(data.get("end_time"))
    clip_format = normalize_format(data)
    subtitles = data.get("subtitles") if isinstance(data.get("subtitles"), list) else []
    sub_offset = float(data.get("sub_offset") or 0.0)

    if not url:
        return jsonify({"error": "Paramètre 'url' manquant."}), 400
    if start_time is None or end_time is None or end_time <= start_time:
        return jsonify({"error": "Paramètres 'start_time' et 'end_time' invalides."}), 400

    duration = end_time - start_time
    dl_start = max(0, start_time - 2)
    dl_end = end_time + 2

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = os.path.join(tmpdir, "raw.mp4")
        out_path = os.path.join(tmpdir, "clip_9x16.mp4")
        ass_path = os.path.join(tmpdir, "subtitles.ass")

        yt_base_cmd = [
            "yt-dlp",
            "--no-playlist",
            "-f",
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/bestvideo[height<=720]+bestaudio/best[height<=720]/best",
            "--merge-output-format",
            "mp4",
        ]
        if os.path.isfile(COOKIES_PATH) and os.path.getsize(COOKIES_PATH) > 0:
            yt_base_cmd.extend(["--cookies", COOKIES_PATH])
        elif os.path.isfile("cookies.txt") and os.path.getsize("cookies.txt") > 0:
            yt_base_cmd.extend(["--cookies", "cookies.txt"])

        yt_section_cmd = yt_base_cmd + [
            "--download-sections",
            f"*{dl_start}-{dl_end}",
            "-o",
            raw_path,
            url,
        ]

        yt_full_cmd = yt_base_cmd + [
            "-o",
            raw_path,
            url,
        ]

        try:
            print("yt-dlp début", flush=True)
            ff_ss = max(0.0, start_time - dl_start)
            try:
                yt_proc = subprocess.run(
                    yt_section_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=240,
                )
            except subprocess.TimeoutExpired:
                return jsonify({"error": "Téléchargement trop long"}), 500

            if (
                yt_proc.returncode != 0
                or not os.path.isfile(raw_path)
                or os.path.getsize(raw_path) == 0
            ):
                if os.path.exists(raw_path):
                    os.remove(raw_path)
                ff_ss = start_time
                try:
                    yt_proc = subprocess.run(
                        yt_full_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=240,
                    )
                except subprocess.TimeoutExpired:
                    return jsonify({"error": "Téléchargement trop long"}), 500

            print("yt-dlp fini", flush=True)

            if (
                yt_proc.returncode != 0
                or not os.path.isfile(raw_path)
                or os.path.getsize(raw_path) == 0
            ):
                err_msg = (yt_proc.stderr or yt_proc.stdout or "Échec yt-dlp").strip()
                return jsonify({"error": f"Erreur téléchargement YouTube : {err_msg[-500:]}"}), 500

            # Aucun titre n'est jamais incrusté.
            # Sous-titres autorisés uniquement pour le format "zoom".
            if clip_format == "bars":
                filter_complex = "[0:v]scale=720:-2,pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,setsar=1[vout]"
            elif clip_format == "crop":
                filter_complex = (
                    "[0:v]scale=720:1280:force_original_aspect_ratio=increase,"
                    "crop=720:1280,setsar=1[vout]"
                )
            else:
                has_ass = build_ass_file(subtitles, sub_offset, ass_path)
                base_zoom = (
                    "[0:v]scale=90:160:force_original_aspect_ratio=increase,"
                    "crop=90:160,boxblur=5:1,scale=720:1280,setsar=1[bg];"
                    "[0:v]crop=iw*0.85:ih*0.85,scale=720:-2,setsar=1[fg];"
                    "[bg][fg]overlay=(W-w)/2:(H-h)/2"
                )
                if has_ass:
                    escaped_ass = (
                        ass_path.replace("\\", "\\\\")
                        .replace(":", "\\:")
                        .replace("'", "\\'")
                    )
                    filter_complex = f"{base_zoom},ass='{escaped_ass}'[vout]"
                else:
                    filter_complex = f"{base_zoom}[vout]"

            ff_cmd = [
                "ffmpeg",
                "-y",
                "-ss",
                f"{ff_ss:.3f}",
                "-i",
                raw_path,
                "-t",
                f"{duration:.3f}",
                "-filter_complex",
                filter_complex,
                "-map",
                "[vout]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "28",
                "-r",
                "30",
                "-threads",
                "2",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                out_path,
            ]

            print("ffmpeg début", flush=True)
            try:
                ff_proc = subprocess.run(
                    ff_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=240,
                )
            except subprocess.TimeoutExpired:
                return jsonify({"error": "Découpe trop longue"}), 500
            print("ffmpeg fini", flush=True)

            if ff_proc.returncode != 0 or not os.path.isfile(out_path):
                err_msg = (ff_proc.stderr or ff_proc.stdout or "Échec ffmpeg").strip()
                return jsonify({"error": f"Erreur encodage vidéo : {err_msg[-500:]}"}), 500

            with open(out_path, "rb") as f:
                video_bytes = io.BytesIO(f.read())

            video_bytes.seek(0)
            return send_file(
                video_bytes,
                mimetype="video/mp4",
                as_attachment=True,
                download_name=f"clip_{clip_format}_720x1280.mp4",
            )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
