import os
import uuid
import glob
import json
import subprocess
import threading
import zipfile
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = ["python", "-m", "yt_dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        if format_id:
            cmd += [
                "-f", f"{format_id}+bestaudio/best",
                "--merge-output-format", "mp4",
            ]
        else:
            cmd += [
                "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio",
                "--merge-output-format", "mp4",
            ]
        # Force AAC audio re-encode for universal compatibility
        cmd += ["--postprocessor-args", "ffmpeg:-c:a aac -b:a 192k"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            # Get the most meaningful error line
            stderr_lines = [l for l in result.stderr.strip().split("\n") if l.strip()]
            error_msg = stderr_lines[-1] if stderr_lines else "Unknown download error"
            # Also log stdout in case error is there
            if not error_msg or "WARNING" in error_msg:
                stdout_lines = [l for l in result.stdout.strip().split("\n") if "ERROR" in l]
                if stdout_lines:
                    error_msg = stdout_lines[-1]
            job["status"] = "error"
            job["error"] = error_msg
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        video_files = [f for f in files if f.endswith(".mp4")]
        audio_files = [f for f in files if f.endswith(".mp3")]
        subtitle_files = [f for f in files if f.endswith(".srt")]

        primary = (video_files[0] if video_files else (audio_files[0] if audio_files else files[0]))

        job["status"] = "transcribing"
        whisper_srt_files = []

        # Only attempt transcription if whisper is importable
        try:
            import importlib.util
            if importlib.util.find_spec("whisper") is not None:
                audio_temp = os.path.join(DOWNLOAD_DIR, f"{job_id}_whisper_input.wav")
                try:
                    extract_cmd = [
                        "ffmpeg", "-y", "-i", primary,
                        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                        audio_temp
                    ]
                    extract_result = subprocess.run(
                        extract_cmd, capture_output=True, timeout=60
                    )
                    
                    if extract_result.returncode == 0 and os.path.exists(audio_temp):
                        input_stem = os.path.splitext(os.path.basename(audio_temp))[0]
                        
                        # English
                        subprocess.run([
                            "python", "-m", "whisper", audio_temp,
                            "--language", "en", "--task", "transcribe",
                            "--output_format", "srt", "--output_dir", DOWNLOAD_DIR,
                            "--model", "small"
                        ], capture_output=True, timeout=300)
                        
                        en_raw = os.path.join(DOWNLOAD_DIR, f"{input_stem}.srt")
                        en_final = os.path.join(DOWNLOAD_DIR, f"{job_id}_english.srt")
                        if os.path.exists(en_raw):
                            os.rename(en_raw, en_final)
                            whisper_srt_files.append(en_final)
                        
                        # Arabic
                        subprocess.run([
                            "python", "-m", "whisper", audio_temp,
                            "--language", "ar", "--task", "transcribe",
                            "--output_format", "srt", "--output_dir", DOWNLOAD_DIR,
                            "--model", "small"
                        ], capture_output=True, timeout=300)
                        
                        ar_raw = os.path.join(DOWNLOAD_DIR, f"{input_stem}.srt")
                        ar_final = os.path.join(DOWNLOAD_DIR, f"{job_id}_arabic.srt")
                        if os.path.exists(ar_raw):
                            os.rename(ar_raw, ar_final)
                            whisper_srt_files.append(ar_final)
                
                except subprocess.TimeoutExpired:
                    job["whisper_error"] = "Whisper timed out — video saved without transcription"
                except Exception as whisper_err:
                    job["whisper_error"] = str(whisper_err)
                finally:
                    audio_temp_path = os.path.join(DOWNLOAD_DIR, f"{job_id}_whisper_input.wav")
                    if os.path.exists(audio_temp_path):
                        try: os.remove(audio_temp_path)
                        except: pass
        except Exception:
            pass  # Whisper entirely unavailable — skip silently

        # Combine all subtitles
        if whisper_srt_files:
            job["status"] = "packaging"
            zip_path = os.path.join(DOWNLOAD_DIR, f"{job_id}.zip")
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                # Media file
                media_ext = os.path.splitext(primary)[1]
                zipf.write(primary, f"video{media_ext}")
                # Subtitle files — use their already-clean names
                for sub in whisper_srt_files:
                    zipf.write(sub, os.path.basename(sub).replace(f"{job_id}_", ""))
            
            # Cleanup
            for f in [primary] + whisper_srt_files:
                try: os.remove(f)
                except: pass
            
            job["file"] = zip_path
            job["status"] = "done"
            ext = ".zip"
        else:
            job["file"] = primary
            job["status"] = "done"
            ext = os.path.splitext(primary)[1]

        title = job.get("title", "").strip()
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:30].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else f"download{ext}"
        else:
            job["filename"] = f"download{ext}"
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    response.headers['Vary'] = '*'
    return response


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    cmd = ["python", "-m", "yt_dlp", "--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


@app.route("/api/debug/<job_id>")
def debug_job(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    # Disable reloader or at least ignore downloads to prevent job wipes
    app.run(host=host, port=port, debug=True, use_reloader=False)
