import io
import os
import subprocess
import tempfile
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_PATH = os.path.join(BASE_DIR, "cookies.txt")
FONT_PATH = os.path.join(BASE_DIR, "font.ttf")


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


def format_ass_time(seconds: float) -> str:
    total_cs = max(0, int(round(float(seconds) * 100)))
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def build_ass_subtitles(subtitles: list, sub_offset: float = 0.0) -> str:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 480
PlayResY: 854
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,24,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2.5,1,2,28,28,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for item in subtitles:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        try:
            start = max(0.0, float(item.get("start", 0)) + float(sub_offset))
            end = max(start + 0.1, float(item.get("end", 0)) + float(sub_offset))
        except (TypeError, ValueError):
            continue

        safe_text = (
            text.replace("\\", "\\\\")
            .replace("{", "(")
            .replace("}", ")")
            .replace("\n", "\\N")
        )
        lines.append(
            f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Default,,0,0,0,,{safe_text}\n"
        )
    return "".join(lines)


def escape_ffmpeg_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


@app.route("/", methods=["GET"])
def health():
    return jsonify({
        "service": "ClipViral backend",
        "status": "online",
        "cookies": os.path.exists(COOKIES_PATH),
        "font": os.path.exists(FONT_PATH),
        "subtitles": True,
    })


@app.route("/api/clip", methods=["POST", "OPTIONS"])
def create_clip():
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    youtube_url = str(data.get("url", "")).strip()
    if not youtube_url:
        return jsonify({"error": "Lien YouTube manquant."}), 400

    try:
        start_time = float(data.get("start_time", 0))
        end_time = float(data.get("end_time", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Paramètres start_time ou end_time invalides."}), 400

    duration = end_time - start_time
    if start_time < 0 or duration <= 0:
        return jsonify({"error": "Plage temporelle invalide (end_time doit être supérieur à start_time)."}), 400

    # Lit le paramètre "format" en priorité (avec repli sur "crop_mode")
    raw_format = str(data.get("format") or data.get("crop_mode") or "zoom").strip().lower()
    clip_format = "crop" if raw_format in ("crop", "fullscreen", "plein_ecran") else "zoom"

    subtitles = data.get("subtitles") if isinstance(data.get("subtitles"), list) else []
    try:
        sub_offset = float(data.get("sub_offset", 0) or 0)
    except (TypeError, ValueError):
        sub_offset = 0.0

    with tempfile.TemporaryDirectory() as tmpdir:
        source_path = os.path.join(tmpdir, "source.mp4")
        output_path = os.path.join(tmpdir, "output.mp4")
        ass_path = os.path.join(tmpdir, "subs.ass")

        yt_cmd = [
            "yt-dlp",
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
            "--merge-output-format", "mp4",
            "-o", source_path,
        ]
        if os.path.exists(COOKIES_PATH):
            yt_cmd.extend(["--cookies", COOKIES_PATH])
        yt_cmd.append(youtube_url)

        yt_proc = subprocess.run(yt_cmd, capture_output=True, text=True)
        if yt_proc.returncode != 0 or not os.path.exists(source_path):
            err_msg = (yt_proc.stderr or yt_proc.stdout or "Échec yt-dlp").strip()
            return jsonify({"error": f"Téléchargement impossible : {err_msg}"}), 500

        # Aucun titre n'est jamais incrusté
        if clip_format == "crop":
            # 2) Plein écran : vidéo plein écran 9:16 (480x854), sans titre ni sous-titres
            filter_complex = (
                "[0:v]scale=480:854:force_original_aspect_ratio=increase,"
                "crop=480:854,setsar=1[vout]"
            )
        else:
            # 1) Zoom : vidéo zoomée au centre sur fond flou 9:16 (480x854), sans titre
            has_subs = len(subtitles) > 0
            if has_subs:
                with open(ass_path, "w", encoding="utf-8") as f:
                    f.write(build_ass_subtitles(subtitles, sub_offset))
                escaped_ass = escape_ffmpeg_path(ass_path)
                filter_complex = (
                    "[0:v]split=2[bg][fg];"
                    "[bg]scale=480:854:force_original_aspect_ratio=increase,"
                    "crop=480:854,boxblur=20:5[bg_blur];"
                    "[fg]crop=iw*0.85:ih*0.85:(iw-iw*0.85)/2:(ih-ih*0.85)/2,"
                    "scale=480:-2[fg_zoom];"
                    "[bg_blur][fg_zoom]overlay=(W-w)/2:(H-h)/2,"
                    f"subtitles='{escaped_ass}',setsar=1[vout]"
                )
            else:
                filter_complex = (
                    "[0:v]split=2[bg][fg];"
                    "[bg]scale=480:854:force_original_aspect_ratio=increase,"
                    "crop=480:854,boxblur=20:5[bg_blur];"
                    "[fg]crop=iw*0.85:ih*0.85:(iw-iw*0.85)/2:(ih-ih*0.85)/2,"
                    "scale=480:-2[fg_zoom];"
                    "[bg_blur][fg_zoom]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]"
                )

        ff_cmd = [
            "ffmpeg", "-y",
            "-ss", str(start_time),
            "-i", source_path,
            "-t", str(duration),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "0:a?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            output_path,
        ]

        ff_proc = subprocess.run(ff_cmd, capture_output=True, text=True)
        if ff_proc.returncode != 0 or not os.path.exists(output_path):
            err_msg = (ff_proc.stderr or "Erreur ffmpeg").strip()
            return jsonify({"error": f"Erreur découpe FFmpeg : {err_msg[-800:]}"}), 500

        with open(output_path, "rb") as f:
            mp4_bytes = f.read()

    return send_file(
        io.BytesIO(mp4_bytes),
        mimetype="video/mp4",
        as_attachment=True,
        download_name="clip.mp4",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
