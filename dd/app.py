import glob
import time
import os
import shutil
import threading
import uuid
import webbrowser

import yt_dlp
from flask import Flask, Response, jsonify, request, send_file
from werkzeug.exceptions import HTTPException

PORT = 8765
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")

# ffmpeg khud mil jata hai (imageio-ffmpeg se), alag install ki zaroorat nahi
FFMPEG = None
try:
    import imageio_ffmpeg

    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = shutil.which("ffmpeg")

VIDEO_FORMATS = ["mp4", "mkv", "webm", "avi", "mov", "flv"]
AUDIO_FORMATS = {
    "mp3": "mp3",
    "m4a": "m4a",
    "aac": "aac",
    "opus": "opus",
    "ogg": "vorbis",
    "flac": "flac",
    "wav": "wav",
}
LOSSY = {"mp3", "m4a", "aac", "opus", "ogg"}

app = Flask(__name__)
JOBS = {}
MAX_ACTIVE = 3
KEEP_SECONDS = 20 * 60


def cleanup_loop():
    while True:
        time.sleep(120)
        now = time.time()
        for jid in list(JOBS):
            j = JOBS.get(jid)
            if j and now - j.get("created", now) > KEEP_SECONDS:
                shutil.rmtree(os.path.join(DOWNLOAD_DIR, jid), ignore_errors=True)
                JOBS.pop(jid, None)


os.makedirs(DOWNLOAD_DIR, exist_ok=True)
threading.Thread(target=cleanup_loop, daemon=True).start()


@app.errorhandler(Exception)
def handle_error(e):
    code = e.code if isinstance(e, HTTPException) else 500
    return jsonify(error=f"Server error: {e}"), code


@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")


@app.get("/api/ping")
def ping():
    return jsonify(ok=True, ffmpeg=bool(FFMPEG))


@app.post("/api/info")
def info():
    url = (request.get_json(silent=True) or {}).get("url", "").strip()
    if not url:
        return jsonify(error="Please paste a video link first."), 400
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as ydl:
            d = ydl.extract_info(url, download=False)
        heights = sorted(
            {f["height"] for f in d.get("formats", []) if f.get("height")}, reverse=True
        )
        return jsonify(
            title=d.get("title"),
            thumbnail=d.get("thumbnail"),
            uploader=d.get("uploader"),
            duration=d.get("duration"),
            heights=heights,
            ffmpeg=bool(FFMPEG),
        )
    except Exception as e:
        return jsonify(error=str(e)), 400


def build_opts(kind, fmt, height, bitrate, folder, hook):
    opts = {
        "outtmpl": os.path.join(folder, "%(title).150B.%(ext)s"),
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG

    if kind == "audio":
        opts["format"] = "bestaudio/best"
        if not FFMPEG:
            return opts  # conversion mumkin nahi, original audio milegi
        pp = {"key": "FFmpegExtractAudio", "preferredcodec": AUDIO_FORMATS.get(fmt, "mp3")}
        if fmt in LOSSY:
            pp["preferredquality"] = str(bitrate or 192)
        opts["postprocessors"] = [pp]
        return opts

    # video
    h = f"[height<={int(height)}]" if height and height != "best" else ""
    if FFMPEG:
        opts["format"] = f"bv*{h}+ba/b{h}"
        if fmt == "mp4":
            opts["format_sort"] = ["res", "vcodec:h264", "acodec:aac"]
            opts["merge_output_format"] = "mp4"
            opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
        elif fmt == "mkv":
            opts["merge_output_format"] = "mkv"
            opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"}]
        else:  # webm, avi, mov, flv: mkv mein jod kar convert
            opts["merge_output_format"] = "mkv"
            opts["postprocessors"] = [{"key": "FFmpegVideoConvertor", "preferedformat": fmt}]
    else:
        # ffmpeg nahi: sirf ready-made single file formats
        opts["format"] = f"b{h}/best"
    return opts


def run_job(job_id, data):
    job = JOBS[job_id]
    folder = os.path.join(DOWNLOAD_DIR, job_id)
    job["part"] = 0

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if total:
                job["percent"] = round(d["downloaded_bytes"] * 100 / total, 1)
            job["speed"] = (d.get("_speed_str") or "").strip()
            job["status"] = "downloading"
        elif d["status"] == "finished":
            job["part"] += 1
            job["status"] = "processing"

    try:
        opts = build_opts(
            data.get("kind", "video"),
            data.get("format", "mp4"),
            data.get("height"),
            data.get("bitrate"),
            folder,
            hook,
        )
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([data["url"]])
        files = [
            f
            for f in glob.glob(os.path.join(folder, "*"))
            if not f.endswith((".part", ".ytdl", ".temp"))
        ]
        if not files:
            raise RuntimeError("The file could not be created.")
        final = max(files, key=os.path.getmtime)
        job.update(file=final, filename=os.path.basename(final), percent=100, status="done")
    except Exception as e:
        job.update(status="error", error=str(e))


@app.post("/api/download")
def download():
    data = request.get_json(silent=True) or {}
    data["url"] = (data.get("url") or "").strip()
    if not data["url"]:
        return jsonify(error="Please paste a video link first."), 400
    active = sum(1 for j in JOBS.values() if j["status"] in ("starting", "downloading", "processing"))
    if active >= MAX_ACTIVE:
        return jsonify(error="The server is busy. Please try again in a minute."), 429
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"status": "starting", "percent": 0, "speed": "", "part": 0, "created": time.time()}
    threading.Thread(target=run_job, args=(job_id, data), daemon=True).start()
    return jsonify(job_id=job_id)


@app.get("/api/progress/<job_id>")
def progress(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify(error="Job not found."), 404
    return jsonify({k: v for k, v in job.items() if k not in ("file", "created")})


@app.get("/api/file/<job_id>")
def get_file(job_id):
    job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify(error="File is not ready."), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Video Downloader</title>
<style>
  :root { --bg:#f4f6fb; --card:#fff; --text:#1c2333; --muted:#6b7385; --accent:#4f46e5; --accent2:#4338ca; --border:#e3e7f0; --ok:#16a34a; --err:#dc2626; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0f1220; --card:#181c30; --text:#eef0f8; --muted:#9aa3bd; --border:#2a3050; } }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); min-height:100vh; display:flex; justify-content:center; padding:32px 16px; }
  .wrap { width:100%; max-width:660px; }
  h1 { margin:0 0 4px; font-size:28px; }
  .sub { color:var(--muted); margin:0 0 22px; }
  .card { background:var(--card); border:1px solid var(--border); border-radius:16px; padding:20px; box-shadow:0 4px 18px rgba(0,0,0,.06); }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  input[type=text], select { flex:1; min-width:120px; padding:12px 14px; border-radius:10px; border:1px solid var(--border); background:var(--bg); color:var(--text); font-size:15px; }
  button { padding:12px 20px; border:0; border-radius:10px; background:var(--accent); color:#fff; font-size:15px; font-weight:600; cursor:pointer; }
  button:hover { background:var(--accent2); }
  button:disabled { opacity:.6; cursor:not-allowed; }
  .preview { display:none; gap:14px; margin-top:18px; align-items:center; }
  .preview img { width:150px; border-radius:10px; aspect-ratio:16/9; object-fit:cover; background:var(--border); }
  .t { font-weight:600; line-height:1.3; } .m { color:var(--muted); font-size:13px; margin-top:4px; }
  .opts { display:none; margin-top:18px; }
  .tabs { display:flex; gap:8px; margin-bottom:12px; }
  .tab { flex:1; background:var(--bg); color:var(--text); border:1px solid var(--border); }
  .tab.on { background:var(--accent); color:#fff; border-color:var(--accent); }
  label { font-size:12px; color:var(--muted); display:block; margin-bottom:4px; }
  .col { flex:1; min-width:120px; }
  .bar { height:10px; background:var(--border); border-radius:99px; overflow:hidden; margin-top:18px; display:none; }
  .bar > div { height:100%; width:0; background:var(--accent); transition:width .3s; }
  .status { margin-top:10px; font-size:14px; color:var(--muted); min-height:20px; word-break:break-word; }
  .status.err { color:var(--err); } .status.ok { color:var(--ok); }
  .save { display:none; margin-top:12px; text-decoration:none; text-align:center; background:var(--ok); color:#fff; padding:12px; border-radius:10px; font-weight:600; }
  .warn { font-size:12px; color:#b45309; margin-top:10px; display:none; }
  .top { position:sticky; top:0; background:var(--card); border-bottom:1px solid var(--border); margin:-32px -16px 28px; padding:14px 16px; z-index:5; }
  .top .inner { max-width:660px; margin:0 auto; display:flex; justify-content:space-between; align-items:center; }
  .top nav a { margin-left:16px; color:var(--muted); text-decoration:none; font-size:14px; }
  .sec { margin-top:34px; } .sec h2 { font-size:20px; margin:0 0 12px; }
  .chips { display:flex; flex-wrap:wrap; gap:8px; } .chips span { background:var(--card); border:1px solid var(--border); padding:6px 12px; border-radius:99px; font-size:13px; font-weight:600; }
  details { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:12px 16px; margin-bottom:8px; }
  summary { cursor:pointer; font-weight:600; } details p { color:var(--muted); margin:8px 0 0; }
  body { display:block; padding-top:32px; } .wrap { margin:0 auto; }
  html { scroll-behavior:smooth; }
  .foot { text-align:center; color:var(--muted); font-size:12px; margin-top:18px; }
</style>
</head>
<body>
<header class="top"><div class="inner"><b>&#9660; Video Downloader</b><nav><a href="#formats">Formats</a><a href="#faq">FAQ</a></nav></div></header>
<div class="wrap">
  <h1>Download videos &amp; audio in any format</h1>
  <p class="sub">Paste a link, choose a format and download.</p>
  <div class="card">
    <div class="row">
      <input id="url" type="text" placeholder="https://...">
      <button id="checkBtn">Check</button>
    </div>

    <div class="preview" id="preview">
      <img id="thumb" alt="">
      <div><div class="t" id="title"></div><div class="m" id="meta"></div></div>
    </div>

    <div class="opts" id="opts">
      <div class="tabs">
        <button class="tab on" id="tabVideo">Video</button>
        <button class="tab" id="tabAudio">Audio</button>
      </div>
      <div class="row" id="videoOpts">
        <div class="col"><label>Format</label>
          <select id="vFormat">
            <option>mp4</option><option>mkv</option><option>webm</option>
            <option>avi</option><option>mov</option><option>flv</option>
          </select></div>
        <div class="col"><label>Quality</label><select id="vHeight"></select></div>
      </div>
      <div class="row" id="audioOpts" style="display:none">
        <div class="col"><label>Format</label>
          <select id="aFormat">
            <option>mp3</option><option>m4a</option><option>aac</option>
            <option>opus</option><option>ogg</option><option>flac</option><option>wav</option>
          </select></div>
        <div class="col"><label>Bitrate (kbps)</label>
          <select id="aRate"><option>320</option><option selected>192</option><option>128</option><option>96</option></select></div>
      </div>
      <div class="row" style="margin-top:14px"><button id="dlBtn" style="flex:1">Download</button></div>
      <div class="warn" id="noff">ffmpeg is not available, so only ready-made formats can be downloaded.</div>
    </div>

    <div class="bar" id="bar"><div id="fill"></div></div>
    <div class="status" id="status"></div>
    <a class="save" id="save" href="#">Save to device</a>
  </div>
  <section id="formats" class="sec"><h2>Supported formats</h2>
    <div class="chips"><span>MP4</span><span>MKV</span><span>WEBM</span><span>AVI</span><span>MOV</span><span>FLV</span><span>MP3</span><span>M4A</span><span>AAC</span><span>OPUS</span><span>OGG</span><span>FLAC</span><span>WAV</span></div>
    <p class="m">Choose the quality you want, from the lowest up to the best the video offers.</p></section>
  <section id="faq" class="sec"><h2>FAQ</h2>
    <details><summary>How do I download a video?</summary><p>Paste the link, press Check, pick Video or Audio, choose the format and press Download. When it is ready, press Save to device.</p></details>
    <details><summary>Why did my download fail?</summary><p>Some sites block downloads or the video may be private. Try another link or a lower quality.</p></details>
    <details><summary>Are my files stored?</summary><p>Files are deleted automatically a short time after they are created.</p></details>
  </section>
  <div class="foot">Only download content you own or have permission to download. You are responsible for following each site&#39;s terms and copyright law.</div>
</div>

<script>
const $ = id => document.getElementById(id);
let kind = "video";
const setStatus = (t, cls="") => { $("status").textContent = t; $("status").className = "status " + cls; };
const fmt = s => { if(!s) return ""; const m=Math.floor(s/60), r=s%60; return m+":"+String(r).padStart(2,"0"); };

async function readJson(r) {
  const text = await r.text();
  try { return JSON.parse(text); }
  catch (_) { throw new Error("The server did not respond correctly (" + r.status + "). Please reload the page and try again."); }
}
async function post(url, body) {
  const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
  const d = await readJson(r);
  if (!r.ok) throw new Error(d.error || "Error");
  return d;
}

function setKind(k) {
  kind = k;
  $("tabVideo").classList.toggle("on", k === "video");
  $("tabAudio").classList.toggle("on", k === "audio");
  $("videoOpts").style.display = k === "video" ? "flex" : "none";
  $("audioOpts").style.display = k === "audio" ? "flex" : "none";
}
$("tabVideo").onclick = () => setKind("video");
$("tabAudio").onclick = () => setKind("audio");

$("checkBtn").onclick = async () => {
  const url = $("url").value.trim();
  if (!url) return setStatus("Please paste a video link first.", "err");
  $("checkBtn").disabled = true; setStatus("Fetching video details...");
  $("save").style.display = "none"; $("bar").style.display = "none";
  try {
    const d = await post("/api/info", {url});
    $("thumb").src = d.thumbnail || "";
    $("title").textContent = d.title || "";
    $("meta").textContent = [d.uploader, fmt(d.duration)].filter(Boolean).join(" • ");
    const sel = $("vHeight"); sel.innerHTML = '<option value="best">Best available</option>';
    (d.heights || []).forEach(h => sel.insertAdjacentHTML("beforeend", `<option value="${h}">${h}p</option>`));
    $("noff").style.display = d.ffmpeg ? "none" : "block";
    $("preview").style.display = "flex"; $("opts").style.display = "block";
    setStatus("");
  } catch (e) { setStatus(e.message, "err"); }
  $("checkBtn").disabled = false;
};

$("dlBtn").onclick = async () => {
  $("dlBtn").disabled = true; $("save").style.display = "none";
  $("bar").style.display = "block"; $("fill").style.width = "0%";
  setStatus("Starting...");
  const body = { url: $("url").value.trim(), kind,
    format: kind === "video" ? $("vFormat").value : $("aFormat").value,
    height: $("vHeight").value, bitrate: $("aRate").value };
  try {
    const {job_id} = await post("/api/download", body);
    const timer = setInterval(async () => {
      let p;
      try { p = await readJson(await fetch("/api/progress/" + job_id)); }
      catch (e) { clearInterval(timer); setStatus(e.message, "err"); $("dlBtn").disabled = false; return; }
      $("fill").style.width = (p.percent || 0) + "%";
      if (p.status === "downloading") setStatus("Downloading... " + (p.percent||0) + "% " + (p.speed ? "("+p.speed+")" : ""));
      else if (p.status === "processing") setStatus("Processing / converting...");
      else if (p.status === "done") {
        clearInterval(timer); setStatus("Ready: " + p.filename, "ok");
        $("save").href = "/api/file/" + job_id; $("save").style.display = "block"; $("dlBtn").disabled = false;
      } else if (p.status === "error") {
        clearInterval(timer); setStatus(p.error || "Something went wrong.", "err"); $("dlBtn").disabled = false;
      }
    }, 700);
  } catch (e) { setStatus(e.message, "err"); $("dlBtn").disabled = false; }
};
$("url").addEventListener("keydown", e => { if (e.key === "Enter") $("checkBtn").click(); });
</script>
</body>
</html>
"""

if __name__ == "__main__":
    port = int(os.environ.get("PORT", PORT))
    online = "PORT" in os.environ
    if not online:
        print(f"\nRunning at: http://127.0.0.1:{port}\n")
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    app.run(host="0.0.0.0" if online else "127.0.0.1", port=port, debug=False)
