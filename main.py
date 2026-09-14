import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from urllib.parse import urlparse, urljoin
import builtins

_print = builtins.print

def print(*args, **kwargs):  # noqa: A001 - intentional shadow: timestamp every log line
    _print(time.strftime("[%H:%M:%S]"), *args, **kwargs)


MPV_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mpv-bin")
os.environ["PATH"] = MPV_DIR + os.pathsep + os.environ["PATH"]

# Add venv Scripts to PATH for yt-dlp CLI access
_vdir = os.path.dirname(os.path.abspath(__file__))
_venv_scripts = os.path.join(_vdir, ".venv", "Scripts")
if os.path.isdir(_venv_scripts) and _venv_scripts not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _venv_scripts + os.pathsep + os.environ["PATH"]

if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
    os.add_dll_directory(MPV_DIR)

import mpv as _mpv
import psutil
import soco
import yt_dlp
from dotenv import load_dotenv
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from PySide6.QtCore import Qt, QTimer, QRect, Signal
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QComboBox, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

load_dotenv()
sonos_ip = os.getenv("SONOS_IP")
stream_port = int(os.getenv("STREAM_PORT", 8002))

audio_uri = None
audio_seek_offset = 0
audio_headers = {}
sonos_playing = False
ffmpeg_process = None
ffmpeg_lock = threading.Lock()
seek_base_pos = 0.0
selected_audio_track = -1   # -1 = default (stream 0), 0+ = specific track index
_last_yt_error = None
_stream_gen = 0                    # bumped on every set_audio_stream() call
_first_byte_event = None           # Event set by the handler when the first audio chunk is written
_stream_output_seek_only = False   # set by seek worker fallback: source can't range-seek
_stream_lock = threading.Lock()
_ffmpeg_stderr_tail = []           # last ffmpeg stderr lines, updated by the drain thread


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


PC_IP = get_local_ip()
speaker = soco.SoCo(sonos_ip)

try:
    p = psutil.Process(os.getpid())
    p.nice(psutil.HIGH_PRIORITY_CLASS)
except (Exception,):
    pass


def free_port(port):
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"],
            text=True, stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                parts = line.split()
                pid = int(parts[-1])
                if pid == os.getpid():
                    continue
                try:
                    proc = psutil.Process(pid)
                    print(f"[CLEANUP] Killing PID {pid} ({proc.name()}) on port {port}")
                    proc.terminate()
                    proc.wait(timeout=3)
                except (Exception,):
                    pass
                return True
    except (Exception,):
        pass
    return False

free_port(stream_port)


def find_ffmpeg():
    ffmpeg_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg")
    if os.path.isdir(ffmpeg_root):
        for build_dir in glob.glob(os.path.join(ffmpeg_root, "ffmpeg-*-essentials_build")):
            candidate = os.path.join(build_dir, "bin", "ffmpeg.exe")
            if os.path.isfile(candidate):
                return candidate
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return ffmpeg_path
    return None


def find_ffprobe():
    ffmpeg_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg")
    if os.path.isdir(ffmpeg_root):
        for build_dir in glob.glob(os.path.join(ffmpeg_root, "ffmpeg-*-essentials_build")):
            candidate = os.path.join(build_dir, "bin", "ffprobe.exe")
            if os.path.isfile(candidate):
                return candidate
    ffprobe_path = shutil.which("ffprobe")
    if ffprobe_path:
        return ffprobe_path
    return None


FFMPEG_PATH = find_ffmpeg()
if not FFMPEG_PATH:
    print("ERROR: ffmpeg not found.")
    sys.exit(1)
print("FFmpeg:", FFMPEG_PATH)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def wait_for_sonos_audio(play_timeout=60.0, audio_timeout=12.0, fail_fast_timeout=10.0):
    """Wait for Sonos PLAYING plus actual audio bytes flowing from the stream.

    Returns "ok", "timeout" (Sonos never reached PLAYING), or "no_audio".
    Fail-fast: if ffmpeg died without ever producing audio, return "no_audio"
    after fail_fast_timeout instead of waiting out the full Sonos timeout.
    """
    started = time.time()
    ever_playing = False
    deadline = started + play_timeout
    while time.time() < deadline:
        try:
            info = speaker.get_current_transport_info()
            if info.get("current_transport_state", "") == "PLAYING":
                ever_playing = True
                break
        except (Exception,):
            pass
        with ffmpeg_lock:
            proc = ffmpeg_process
        if (not ever_playing and proc is None
                and (time.time() - started) >= fail_fast_timeout):
            print("[SYNC] ffmpeg died without producing audio — failing fast")
            return "no_audio"
        time.sleep(0.5)
    if not ever_playing:
        print("[SYNC] Sonos did not start playing in time")
        return "timeout"
    with _stream_lock:
        ev = _first_byte_event
    if ev is None:
        return "ok"
    if ev.wait(timeout=audio_timeout):
        return "ok"
    print("[SYNC] Sonos PLAYING but no audio bytes within timeout")
    return "no_audio"


_settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

def load_settings():
    try:
        with open(_settings_file) as f:
            return json.load(f)
    except (Exception,):
        return {}

def save_settings(data):
    try:
        with open(_settings_file, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[SETTINGS] Failed to save: {e}")


SYNC_OFFSET_SECONDS = 0.0
try:
    SYNC_OFFSET_SECONDS = float(load_settings().get("sync_offset_seconds", 0.0) or 0.0)
except (Exception,):
    SYNC_OFFSET_SECONDS = 0.0


def parse_sonos_position(pos_str):
    if not pos_str or pos_str == 'NOT_IMPLEMENTED':
        return 0.0
    try:
        parts = list(map(int, pos_str.split(':')))
        if len(parts) == 3:
            return float(parts[0] * 3600 + parts[1] * 60 + parts[2])
        if len(parts) == 2:
            return float(parts[0] * 60 + parts[1])
        if len(parts) == 1:
            return float(parts[0])
    except (Exception,):
        pass
    return 0.0


class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.handle_stream(head_only=False)

    def do_HEAD(self):
        self.handle_stream(head_only=True)

    def handle_stream(self, head_only=False):
        global ffmpeg_process, selected_audio_track, audio_headers
        if self.path != "/stream.mp3":
            self.send_error(404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.end_headers()

        if head_only:
            return

        if not audio_uri:
            print("[STREAM] No audio URI set")
            return

        with _stream_lock:
            stream_gen = _stream_gen
            first_byte_event = _first_byte_event
            output_seek_only = _stream_output_seek_only and audio_uri.startswith("http")
        t0 = time.time()

        merged_headers = dict(audio_headers) if audio_headers else {}
        if audio_uri.startswith("http"):
            if not any(k.lower() == "user-agent" for k in merged_headers):
                merged_headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
            if ("youtube.com" in audio_uri or "googlevideo.com" in audio_uri) \
                    and not any(k.lower() in ("origin", "referer") for k in merged_headers):
                merged_headers["Origin"] = "https://www.youtube.com"
                merged_headers["Referer"] = "https://www.youtube.com/"
        headers_arg = "".join(f"{k}: {v}\r\n" for k, v in merged_headers.items()) if merged_headers else None
        ua_present = any(k.lower() == "user-agent" for k in merged_headers)
        print(f"[STREAM] HTTP headers: {', '.join(sorted(merged_headers.keys())) or 'none'} (UA: {'yes' if ua_present else 'NO'})")
        fallback_output_seek = output_seek_only and audio_uri.startswith("http")
        cmd = [FFMPEG_PATH, "-re"]
        if headers_arg:
            cmd += ["-headers", headers_arg]
        if audio_seek_offset > 0 and not fallback_output_seek:
            cmd += ["-ss", str(audio_seek_offset)]      # input seek (fast) for local and http
        cmd += ["-i", audio_uri]
        if audio_seek_offset > 0 and fallback_output_seek:
            cmd += ["-ss", str(audio_seek_offset)]      # fallback: realtime output seek after -i
        if selected_audio_track >= 0 and not audio_uri.startswith("http"):
            cmd += ["-map", f"0:a:{selected_audio_track}"]
            print(f"[STREAM] Mapping audio stream: 0:a:{selected_audio_track}")
        cmd += ["-vn", "-acodec", "libmp3lame", "-ab", "192k",
                "-ac", "2", "-ar", "44100", "-f", "mp3", "pipe:1"]
        print(f"[STREAM] Starting ffmpeg: {audio_uri} (seek_offset={audio_seek_offset}s, gen={stream_gen}, fallback={fallback_output_seek})")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            print("[STREAM] Failed to start ffmpeg:", e)
            return

        _ffmpeg_stderr_tail.clear()

        def _drain_ffmpeg_stderr():
            try:
                for raw in iter(proc.stderr.readline, b""):
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        _ffmpeg_stderr_tail.append(line)
                        if len(_ffmpeg_stderr_tail) > 40:
                            _ffmpeg_stderr_tail.pop(0)
            except (Exception,):
                pass

        threading.Thread(target=_drain_ffmpeg_stderr, daemon=True).start()

        with ffmpeg_lock:
            ffmpeg_process = proc

        try:
            first_chunk = True
            while True:
                chunk = proc.stdout.read(1024)
                if not chunk:
                    break
                if first_chunk:
                    first_chunk = False
                    print(f"[STREAM] First audio byte after {time.time() - t0:.2f}s (gen {stream_gen})")
                    if first_byte_event is not None:
                        first_byte_event.set()
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            print(f"[STREAM] Connection lost: {type(e).__name__}: {e}")
        finally:
            proc.kill()
            with ffmpeg_lock:
                if ffmpeg_process is proc:
                    ffmpeg_process = None
            print("[STREAM] ffmpeg process ended")
            if first_chunk:
                tail = " | ".join(_ffmpeg_stderr_tail[-6:]) if _ffmpeg_stderr_tail else "no stderr output"
                print(f"[STREAM] No audio was produced — ffmpeg stderr tail: {tail}")

    def log_message(self, format, *args):
        pass


def start_stream_server():
    server = ThreadingHTTPServer(("0.0.0.0", stream_port), StreamHandler)
    print(f"[STREAM] Listening on port {stream_port}")
    server.serve_forever()


def start_sonos_stream():
    stream_uri = "http://" + PC_IP + ":" + str(stream_port) + "/stream.mp3"
    print("[SONOS] Connecting to stream:", stream_uri)
    speaker.stop()
    speaker.play_uri(stream_uri)
    print("[SONOS] play_uri sent")


def stop_sonos_stream():
    global sonos_playing
    sonos_playing = False
    try:
        speaker.stop()
    except (Exception,):
        pass


def set_audio_stream(uri, seek_sec=0, headers=None):
    global audio_uri, audio_seek_offset, audio_headers, _stream_gen, _first_byte_event
    audio_uri = uri
    audio_seek_offset = seek_sec
    audio_headers = headers or {}
    with _stream_lock:
        _stream_gen += 1
        _first_byte_event = threading.Event()
    print(f"[AUDIO] Stream offset set to {seek_sec}s (gen {_stream_gen})")


def _classify_yt_error(err):
    """Return a short code for common yt-dlp failures, or None."""
    msg = str(err).lower()
    if "confirm your age" in msg or "sign in to confirm your age" in msg \
       or "you must confirm your age" in msg or "is age restricted" in msg:
        return "age"
    if "private video" in msg or "private" in msg and "video" in msg:
        return "private"
    if "not available in your country" in msg or "geo" in msg or "in your country" in msg:
        return "geo"
    if "unavailable" in msg or "removed" in msg or "couldn't be found" in msg:
        return "unavailable"
    if "confirm you're not a bot" in msg or "sign in to confirm you're not a bot" in msg or "bot check" in msg:
        return "bot"
    if "javascript runtime" in msg or "js runtime" in msg:
        return "js_runtime"
    return None


def _yt_error_text(code):
    if code == "age":
        return "age-restricted video — YouTube requires sign-in to confirm your age"
    if code == "private":
        return "private video (not accessible)"
    if code == "geo":
        return "video isn't available in your region"
    if code == "unavailable":
        return "video is unavailable or was removed"
    if code == "bot":
        return "video hit a YouTube bot check (see logs)"
    if code == "js_runtime":
        return "YouTube needs a JavaScript runtime for extraction (see yt-dlp logs)"
    return "could not be resolved"


def resolve_media_url(url):
    """Resolve a media URL to direct video + audio CDN URLs.

    Returns (video_url, audio_url, title, duration, is_live, video_headers, audio_headers) or
    (None, None, None, 0, False, {}, {}) on failure.
    """
    global _last_yt_error
    _last_yt_error = None
    try:
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                print("[RESOLVE] Failed to extract info")
                return None, None, None, 0, False, {}, {}
            video_url = info.get("url")
            audio_url = info.get("url")
            title = info.get("title", "Unknown")
            duration = info.get("duration", 0) or 0
            is_live = info.get("is_live", False) or False
            video_headers = {}
            audio_headers = {}
            if "formats" in info:
                best_audio = None
                best_audio_key = None
                for fmt in info["formats"]:
                    if fmt.get("acodec", "none") != "none" and fmt.get("vcodec", "none") == "none":
                        key = _audio_sort_key(fmt)
                        if best_audio is None or key < best_audio_key:
                            best_audio = fmt
                            best_audio_key = key
                if best_audio:
                    print(f"[RESOLVE] Audio pick: {best_audio.get('format_id')} "
                          f"({best_audio.get('abr')}kbps, lang={best_audio.get('language')})")
                audio_headers = best_audio.get("http_headers", {}) if best_audio else {}
                if best_audio:
                    audio_url = best_audio.get("url", audio_url)
                best_video = None
                for fmt in info["formats"]:
                    if fmt.get("vcodec", "none") != "none" and fmt.get("height", 0):
                        if best_video is None or (fmt.get("height", 0) or 0) > (best_video.get("height", 0) or 0):
                            best_video = fmt
                if best_video:
                    video_url = best_video.get("url", video_url)
                    video_headers = best_video.get("http_headers", {})
                else:
                    video_headers = {}
            print(f"[RESOLVE] Resolved: {title} (live={is_live}, dur={duration}s)")
            return video_url, audio_url, title, duration, is_live, video_headers, audio_headers
    except Exception as e:
        print(f"[RESOLVE] Resolution failed: {e}")
        _last_yt_error = _classify_yt_error(e)
        return None, None, None, 0, False, {}, {}


def scrape_page_for_media(url):
    """Fetch a page and extract a playable media URL from its HTML.

    Handles JS-rendered video players that yt-dlp cannot parse.
    Returns a direct media URL string, an iframe embed URL for further
    scraping, or None if nothing is found.
    """
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        # JS config patterns: "file":"..." or "src":"..."
        file_urls = re.findall(r'"file"\s*:\s*"([^"]+)"', html)
        src_urls = re.findall(r'"src"\s*:\s*"([^"]+)"', html)
        video_src = re.findall(r'<video[^>]+src="([^"]+)"', html, re.IGNORECASE)
        source_src = re.findall(r'<source[^>]+src="([^"]+)"', html, re.IGNORECASE)
        iframes = re.findall(r'<iframe[^>]+src="([^"]+)"', html, re.IGNORECASE)

        # Collect candidates in priority order
        candidates = file_urls + src_urls + video_src + source_src + iframes

        media_ext = re.compile(r'\.(m3u8|mp4|webm|ts)(\?|$)', re.IGNORECASE)

        for c in candidates:
            c = c.strip().strip('"\'')
            absolute = urljoin(url, c)
            if media_ext.search(absolute):
                print(f"[SCRAPE] Found media URL: {absolute[:120]}")
                return absolute

        # No direct media URL; return first iframe for recursive scraping
        if iframes:
            iframe_url = urljoin(url, iframes[0].strip().strip('"\''))
            print(f"[SCRAPE] Found iframe embed: {iframe_url}")
            return iframe_url

        print(f"[SCRAPE] No media found in {url[:80]}")
        return None
    except Exception as e:
        print(f"[SCRAPE] Failed to fetch {url[:80]}: {e}")
        return None


def probe_audio_tracks(path_or_url):
    """Probe a local file or URL for available audio tracks.

    Returns a dict with probe results, or None on failure.
    For local files: {"type": "local", "audio_tracks": [...]}
    For URLs: {"type": "url", "video_url": ..., "title": ..., "duration": ...,
               "is_live": ..., "video_headers": ..., "audio_tracks": [...]}
    """
    parsed = urlparse(path_or_url)
    is_url = parsed.scheme in ("http", "https", "ftp", "udp", "rtmp", "rtsp")
    if is_url:
        return _probe_url_audio_tracks(path_or_url)
    else:
        return _probe_local_audio_tracks(path_or_url)


def _is_hdr_transfer(color_transfer):
    """Check if a color_transfer value indicates HDR content."""
    if not color_transfer:
        return False
    ct = color_transfer.lower()
    return ct in ("smpte2084", "arib-std-b67", "smpte428", "pq", "hlg")


def _format_is_hdr(fmt):
    """Return True if a yt-dlp format entry indicates HDR video."""
    if _is_hdr_transfer(fmt.get("color_transfer", "")):
        return True
    prim = (fmt.get("color_primaries") or "").lower()
    if prim in ("bt2020", "bt2020-10", "bt2020-12"):
        return True
    dr = (fmt.get("dynamic_range") or "").lower()
    if "hdr" in dr or "hlg" in dr or "pq" in dr:
        return True
    note = (fmt.get("format_note") or "").lower()
    if "hdr" in note:
        return True
    return False


def _probe_local_audio_tracks(file_path):
    if not os.path.isfile(file_path):
        return None
    ffprobe_path = find_ffprobe()
    if not ffprobe_path:
        return None
    try:
        cmd = [ffprobe_path, "-v", "quiet", "-print_format", "json",
               "-show_streams", "-select_streams", "a", file_path]
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL,
                                      creationflags=CREATE_NO_WINDOW)
        data = json.loads(out)
        streams = data.get("streams", [])
        tracks = []
        for s in streams:
            idx = s.get("index", 0)
            codec = s.get("codec_name", "unknown")
            ch_layout = s.get("channel_layout", "")
            tags = s.get("tags", {}) or {}
            title = tags.get("title", "")
            lang = tags.get("language", "")
            display_parts = []
            if title:
                display_parts.append(title)
            elif lang:
                display_parts.append(lang.upper())
            else:
                display_parts.append(f"Stream {idx}")
            codec_part = codec
            if ch_layout:
                codec_part += f", {ch_layout}"
            display_parts.append(f"({codec_part})")
            display = " ".join(display_parts)
            tracks.append({
                "index": idx,
                "title": display,
                "codec": codec,
                "channels": ch_layout,
                "language": lang,
                "_raw_title": title,
            })
        source_hdr = False
        try:
            vcmd = [ffprobe_path, "-v", "quiet", "-print_format", "json",
                    "-show_streams", "-select_streams", "v", file_path]
            vout = subprocess.check_output(vcmd, text=True, stderr=subprocess.DEVNULL,
                                           creationflags=CREATE_NO_WINDOW)
            vdata = json.loads(vout)
            for vs in vdata.get("streams", []):
                ct = vs.get("color_transfer", "") or ""
                if _is_hdr_transfer(ct):
                    source_hdr = True
                    break
        except (Exception,):
            pass
        return {"type": "local", "audio_tracks": tracks, "is_hdr": source_hdr}
    except Exception as e:
        print(f"[PROBE] Local probe failed: {e}")
        return None


def _audio_sort_key(entry):
    """Sort key for YouTube audio formats: English or original language first,
    then highest average bitrate (lower key = better)."""
    lang = (entry.get("language") or "").lower()
    is_en = 0 if lang.startswith("en") else 1
    lp = entry.get("language_preference")
    original = 0 if (lp is not None and lp < 0) else 1
    abr = entry.get("abr")
    if not isinstance(abr, (int, float)):
        abr = 0
    return (is_en, original, -abr)


def _probe_url_audio_tracks(url):
    global _last_yt_error
    _last_yt_error = None
    try:
        ydl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "skip_download": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info is None:
                return None

            video_url = info.get("url")
            title = info.get("title", "Unknown")
            duration = info.get("duration", 0) or 0
            is_live = info.get("is_live", False) or False

            audio_tracks = []
            formats = info.get("formats", [])

            video_headers = {}
            source_hdr = False
            best_video = None
            for fmt in formats:
                if fmt.get("vcodec", "none") != "none" and fmt.get("height", 0):
                    if _format_is_hdr(fmt):
                        source_hdr = True
                    if best_video is None or (fmt.get("height", 0) or 0) > (best_video.get("height", 0) or 0):
                        best_video = fmt
            if best_video:
                video_headers = best_video.get("http_headers", {})
                if not video_url:
                    video_url = best_video.get("url")

            idx = 0
            for fmt in formats:
                acodec = fmt.get("acodec", "none")
                vcodec = fmt.get("vcodec", "none")
                if acodec != "none" and (vcodec == "none" or vcodec is None):
                    ext = fmt.get("ext", "")
                    abr = fmt.get("abr")
                    format_note = fmt.get("format_note", "")
                    fmt_url = fmt.get("url", "")
                    if not fmt_url:
                        continue
                    parts = [f"Format {fmt.get('format_id', idx)}:"]
                    if ext:
                        parts.append(ext)
                    if abr:
                        parts.append(f"{int(abr)}kbps")
                    elif format_note:
                        parts.append(format_note)
                    if acodec and acodec != "none":
                        parts.append(f"({acodec})")
                    if fmt.get("language"):
                        parts.append(f"[{str(fmt.get('language')).upper()}]")
                    display = " ".join(parts)

                    audio_tracks.append({
                        "index": idx,
                        "format_id": fmt.get("format_id", ""),
                        "title": display,
                        "codec": acodec,
                        "channels": "",
                        "language": fmt.get("language", ""),
                        "abr": fmt.get("abr"),
                        "language_preference": fmt.get("language_preference"),
                        "url": fmt_url,
                        "http_headers": fmt.get("http_headers", {}),
                    })
                    idx += 1

            if audio_tracks:
                audio_tracks.sort(key=_audio_sort_key)
                print(f"[PROBE] Default audio: {audio_tracks[0]['title']}")

            return {
                "type": "url",
                "video_url": video_url or "",
                "title": title,
                "duration": duration,
                "is_live": is_live,
                "video_headers": video_headers,
                "audio_tracks": audio_tracks,
                "is_hdr": source_hdr,
            }
    except Exception as e:
        print(f"[PROBE] URL probe failed: {e}")
        _last_yt_error = _classify_yt_error(e)
        return None


DARK_STYLE = """
QMainWindow {
    background-color: #1a1a2e;
    color: #eee;
    font-family: 'Segoe UI', sans-serif;
}
QWidget#centralWidget {
    background-color: #1a1a2e;
}
QLabel { color: #eee; }
QPushButton {
    background-color: #0f3460;
    color: #a0c4ff;
    border: none;
    border-radius: 6px;
    padding: 8px 18px;
    font-size: 13px;
    font-weight: 500;
}
QPushButton:hover { background-color: #1a4a7a; }
QPushButton:disabled { background-color: #1a2540; color: #555; }
QPushButton#stopBtn {
    background-color: #4a1020;
    color: #ff6b8a;
}
QPushButton#stopBtn:hover { background-color: #6a1830; }
QPushButton#browseBtn {
    background-color: #16213e;
    color: #a0c4ff;
}
QSlider::groove:horizontal {
    height: 4px;
    background: #0f3460;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px;
    height: 14px;
    margin: -5px 0;
    background: #a0c4ff;
    border-radius: 7px;
}
QSlider::sub-page:horizontal {
    background: #a0c4ff;
    border-radius: 2px;
}
QLineEdit {
    background-color: #0a0f1e;
    color: #eee;
    border: 1px solid #0f3460;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 13px;
}
QLineEdit:focus { border-color: #a0c4ff; }
QWidget#titleBar {
    background-color: #0d1b2a;
    border-bottom: 1px solid #0f3460;
}
QPushButton#minBtn {
    background-color: transparent;
    color: #a0c4ff;
    border: none;
    border-radius: 4px;
    font-size: 14px;
    font-weight: bold;
}
QPushButton#minBtn:hover { background-color: #0f3460; }
QPushButton#closeBtn {
    background-color: transparent;
    color: #ff6b8a;
    border: none;
    border-radius: 4px;
    font-size: 14px;
    font-weight: bold;
}
QPushButton#closeBtn:hover { background-color: #4a1020; }
QPushButton#hdrBtn {
    background-color: #16213e;
    color: #a0c4ff;
    border: 1px solid #0f3460;
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 12px;
}
QPushButton#hdrBtn:checked {
    background-color: #1a3a1a;
    color: #6aff6a;
    border: 1px solid #2a5a2a;
}
QSpinBox {
    background-color: #0a0f1e;
    color: #eee;
    border: 1px solid #0f3460;
    border-radius: 4px;
    padding: 4px 6px;
    font-size: 12px;
}
QSpinBox:focus { border-color: #a0c4ff; }
QSpinBox:disabled {
    background-color: #1a2540;
    color: #555;
    border-color: #1a2540;
}
QSpinBox::up-button, QSpinBox::down-button {
    background-color: #0f3460;
    border: none;
    width: 16px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background-color: #1a4a7a;
}
QComboBox {
    background-color: #0a0f1e;
    color: #eee;
    border: 1px solid #0f3460;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
    min-height: 20px;
}
QComboBox:focus { border-color: #a0c4ff; }
QComboBox:disabled {
    background-color: #1a2540;
    color: #555;
    border-color: #1a2540;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 20px;
    border-left: 1px solid #0f3460;
    border-top-right-radius: 4px;
    border-bottom-right-radius: 4px;
}
QComboBox::down-arrow {
    width: 10px;
    height: 10px;
}
QComboBox QAbstractItemView {
    background-color: #0a0f1e;
    color: #eee;
    border: 1px solid #0f3460;
    selection-background-color: #0f3460;
    selection-color: #a0c4ff;
    outline: none;
}
QPushButton#subBtn {
    background-color: #16213e;
    color: #a0c4ff;
    border: 1px solid #0f3460;
    border-radius: 6px;
    padding: 8px 14px;
    font-size: 12px;
}
QPushButton#subBtn:hover { background-color: #1a4a7a; }
QPushButton#subBtn:checked {
    background-color: #1a3a1a;
    color: #6aff6a;
    border: 1px solid #2a5a2a;
}
"""


def fmt_time(ms):
    if ms < 0:
        ms = 0
    total_sec = ms // 1000
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class MainWindow(QMainWindow):
    _status_signal = Signal(str)
    _schedule_color_log = Signal()
    _sonos_pos_signal = Signal(str, float)
    _probe_signal = Signal(object)
    _playback_end_signal = Signal()

    # --- Sync tuning constants (safe to adjust) ---
    SONOS_POLL_INTERVAL = 0.2        # monitor loop tick (seconds)
    SONOS_STALL_TICKS = 8            # consecutive non-playing ticks before auto-restart (~2s)
    EXTRAP_CAP = 2.0                 # max seconds to extrapolate past the last Sonos reading
    DRIFT_DEADBAND = 0.05            # |drift| <= this -> fully disengaged, speed 1.0
    DRIFT_ENGAGE = 0.15              # engage corrections only above this (hysteresis)
    DRIFT_RELEASE = 0.06             # release (disengage) when engaged and |drift| <= this
    DRIFT_FINE = 0.3                 # fine zone upper bound
    SPEED_FINE_DIV = 8.0             # fine zone speed = 1 - fd/SPEED_FINE_DIV (gentle)
    SPEED_FINE_CLAMP = 0.025         # fine zone max speed deviation from base
    REANCHOR_SETTLE = 2.5            # correction holdoff after init-sync/re-anchor (seconds)
    SPEED_CHANGE_MIN_INTERVAL = 0.6  # min seconds between actual mpv speed writes
    OFFSET_TRIM_MIN = 0.04           # auto-trim anchor when quiet median drift exceeds this
    OFFSET_SPREAD_MAX = 0.12         # max sample spread (max-min) for a stable window
    RATE_MIN_PPM = 100               # apply rate feed-forward only above this
    RATE_MAX_PPM = 5000              # sanity cap on believable rate error
    RATE_BIAS_CLAMP = 0.001          # max |rate bias| (fractional speed)
    LEARN_QUIET = 45.0               # corrections must be idle this long before learning
    LEARN_MIN_WINDOW = 60.0          # min clean sample-window span (seconds)
    TELEMETRY_INTERVAL = 60.0        # drift telemetry print interval
    PLAY_WARMUP_GRACE = 15.0         # suppress re-anchors during play-start decode warmup
    SONOS_STALL_SECS = 1.6           # Sonos reading older than this -> stall: hold video, skip corrections
    SONOS_STALL_RESTART_AFTER = 20.0 # stall this long -> force stream restart at last known position
    DRIFT_REANCHOR = 2.0             # |drift| > this -> hard re-anchor; below this the speed corrector catches up seamlessly
    REANCHOR_THROTTLE = 3.0          # min seconds between re-anchors
    SPEED_COARSE_DIV = 3.0           # coarse zone speed = base - fd/SPEED_COARSE_DIV
    SPEED_COARSE_CLAMP = 0.28        # coarse zone max speed deviation from base

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sonos PC Streamer")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(720, 200)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self._player = None
        self._current_uri = None
        self._duration_ms = 0
        self._seeking = False
        self._drag_pos = None
        self._dragging = False
        self._resize_edge = None
        self._RESIZE_MARGIN = 6
        self._is_live = False
        self._resolved_audio_headers = {}
        self._hdr_enabled = load_settings().get("hdr_enabled", False)
        self._source_is_hdr = False
        self._nits = load_settings().get("nits", 1000)
        self._sync_ready = False
        self._initial_sync_done = False
        self._drift_f = 0.0
        self._last_resync = 0.0
        self._drift_hist = []
        self._corr_engaged = False
        self._last_speed_write = 0.0
        self._sync_rate_bias = 0.0
        self._last_poll_time = 0.0
        self._sync_offset_runtime = 0.0
        self._user_sync_offset = SYNC_OFFSET_SECONDS
        self._last_open_dir = load_settings().get("last_open_dir", "")
        self._fd_samples = []
        self._last_engage_time = 0.0
        self._last_telemetry = 0.0
        self._play_started_at = 0.0
        self._corrections_count = 0
        self._stall_active = False
        self._stall_started_at = 0.0
        self._current_speed = 1.0
        self._seek_gen = 0
        self._seek_in_progress = False
        self._av_lock = threading.Lock()
        self._correction_holdoff_until = 0.0
        self._last_soco_elapsed = 0.0
        self._last_soco_time = 0.0
        self._audio_tracks = []
        self._probe_data = None
        self._last_probed_uri = ""
        self._probe_error_reason = None
        self._selected_audio_index = -1
        self._subtitle_tracks = []
        self._sub_last_track_ids = ()

        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Custom title bar
        title_bar = QWidget()
        title_bar.setFixedHeight(36)
        title_bar.setObjectName("titleBar")
        title_bar_layout = QHBoxLayout(title_bar)
        title_bar_layout.setContentsMargins(12, 0, 4, 0)
        title_bar_layout.setSpacing(0)

        title_label = QLabel("Sonos PC Streamer")
        title_label.setStyleSheet("font-size: 13px; font-weight: bold; color: #a0c4ff; background: transparent;")
        title_bar_layout.addWidget(title_label)
        title_bar_layout.addStretch()

        min_btn = QPushButton(chr(0x2014))  # em dash as minimize icon
        min_btn.setObjectName("minBtn")
        min_btn.setFixedSize(36, 28)
        min_btn.clicked.connect(self.showMinimized)
        title_bar_layout.addWidget(min_btn)

        close_btn = QPushButton(chr(0x2715))  # x mark
        close_btn.setObjectName("closeBtn")
        close_btn.setFixedSize(36, 28)
        close_btn.clicked.connect(self.close)
        title_bar_layout.addWidget(close_btn)

        layout.addWidget(title_bar)

        content_area = QWidget()
        content_layout = QVBoxLayout(content_area)
        content_layout.setContentsMargins(16, 4, 16, 8)
        content_layout.setSpacing(4)

        file_row = QHBoxLayout()
        self.uri_label = QLineEdit()
        self.uri_label.setPlaceholderText("File path or URL...")
        file_row.addWidget(self.uri_label, stretch=1)
        self.browse_btn = QPushButton("Browse")
        self.browse_btn.setObjectName("browseBtn")
        self.browse_btn.setFixedWidth(80)
        self.browse_btn.clicked.connect(self.browse_file)
        file_row.addWidget(self.browse_btn)
        content_layout.addLayout(file_row)

        transport_row = QHBoxLayout()
        self.play_btn = QPushButton("Play")
        self.play_btn.setFixedWidth(90)
        self.play_btn.clicked.connect(self.on_play)
        transport_row.addWidget(self.play_btn)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setFixedWidth(90)
        self.stop_btn.clicked.connect(self.on_stop)
        transport_row.addWidget(self.stop_btn)
        self.fs_btn = QPushButton("Fullscreen")
        self.fs_btn.setObjectName("browseBtn")
        self.fs_btn.clicked.connect(self.on_fullscreen)
        transport_row.addWidget(self.fs_btn)
        self.hdr_btn = QPushButton("HDR: ON" if self._hdr_enabled else "HDR: OFF")
        self.hdr_btn.setCheckable(True)
        self.hdr_btn.setChecked(self._hdr_enabled)
        self.hdr_btn.setObjectName("hdrBtn")
        self.hdr_btn.toggled.connect(self._toggle_hdr)
        transport_row.addWidget(self.hdr_btn)
        self.nits_label = QLabel(" Nits:")
        self.nits_label.setStyleSheet("color: #888; font-size: 12px;")
        transport_row.addWidget(self.nits_label)
        self.nits_spin = QSpinBox()
        self.nits_spin.setRange(200, 10000)
        self.nits_spin.setSingleStep(50)
        self.nits_spin.setValue(self._nits)
        self.nits_spin.setFixedWidth(80)
        self.nits_spin.setEnabled(self._hdr_enabled)
        self.nits_spin.valueChanged.connect(self._on_nits_changed)
        transport_row.addWidget(self.nits_spin)
        transport_row.addStretch()
        content_layout.addLayout(transport_row)

        audio_track_row = QHBoxLayout()
        self.audio_track_label = QLabel("Audio Track:")
        self.audio_track_label.setStyleSheet("color: #888; font-size: 12px;")
        audio_track_row.addWidget(self.audio_track_label)
        self.audio_track_combo = QComboBox()
        self.audio_track_combo.setMinimumWidth(260)
        self.audio_track_combo.addItem("Default (auto)", -1)
        self.audio_track_combo.setEnabled(False)
        self.audio_track_combo.currentIndexChanged.connect(self._on_audio_track_changed)
        audio_track_row.addWidget(self.audio_track_combo, stretch=1)
        content_layout.addLayout(audio_track_row)

        sub_row = QHBoxLayout()
        self.sub_label = QLabel("Subtitles:")
        self.sub_label.setStyleSheet("color: #888; font-size: 12px;")
        sub_row.addWidget(self.sub_label)
        self.sub_combo = QComboBox()
        self.sub_combo.setMinimumWidth(260)
        self.sub_combo.addItem("No subtitles", -1)
        self.sub_combo.setEnabled(False)
        self.sub_combo.currentIndexChanged.connect(self._on_sub_changed)
        sub_row.addWidget(self.sub_combo, stretch=1)
        self.sub_btn = QPushButton("Subs: ON")
        self.sub_btn.setCheckable(True)
        self.sub_btn.setChecked(True)
        self.sub_btn.setObjectName("subBtn")
        self.sub_btn.toggled.connect(self._on_sub_toggle)
        sub_row.addWidget(self.sub_btn)
        content_layout.addLayout(sub_row)

        time_row = QHBoxLayout()
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("color: #888; font-size: 12px; min-width: 100px;")
        time_row.addWidget(self.time_label)
        self.sonos_label = QLabel("")
        self.sonos_label.setStyleSheet("color: #888; font-size: 11px; min-width: 120px;")
        time_row.addWidget(self.sonos_label)
        time_row.addStretch()
        self.speed_label = QLabel("")
        self.speed_label.setStyleSheet("color: #aaa; font-size: 12px; min-width: 60px;")
        time_row.addWidget(self.speed_label)
        content_layout.addLayout(time_row)

        seek_row = QHBoxLayout()
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)
        seek_row.addWidget(self.seek_slider, stretch=1)
        content_layout.addLayout(seek_row)

        vol_row = QHBoxLayout()
        self.vol_label = QLabel("Volume: 30")
        self.vol_label.setStyleSheet("color: #888; font-size: 12px;")
        vol_row.addWidget(self.vol_label)
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(30)
        self.vol_slider.valueChanged.connect(self.on_volume_changed)
        vol_row.addWidget(self.vol_slider, stretch=1)
        self.sync_offset_label = QLabel(f"Video Sync: {self._user_sync_offset:+.2f}s")
        self.sync_offset_label.setStyleSheet("color: #888; font-size: 12px; min-width: 112px;")
        vol_row.addWidget(self.sync_offset_label)
        self.sync_minus_btn = QPushButton("−")
        self.sync_minus_btn.setObjectName("browseBtn")
        self.sync_minus_btn.setFixedSize(36, 28)
        self.sync_minus_btn.setStyleSheet("padding: 2px 0px; font-size: 14px;")
        self.sync_minus_btn.clicked.connect(self._on_sync_nudge_minus)
        vol_row.addWidget(self.sync_minus_btn)
        self.sync_plus_btn = QPushButton("+")
        self.sync_plus_btn.setObjectName("browseBtn")
        self.sync_plus_btn.setFixedSize(36, 28)
        self.sync_plus_btn.setStyleSheet("padding: 2px 0px; font-size: 14px;")
        self.sync_plus_btn.clicked.connect(self._on_sync_nudge_plus)
        vol_row.addWidget(self.sync_plus_btn)
        content_layout.addLayout(vol_row)

        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("color: #555; font-size: 12px; margin-top: 4px;")
        content_layout.addWidget(self.status_label)
        self._status_signal.connect(self.status_label.setText)
        self._schedule_color_log.connect(self._on_schedule_color_log)
        self._sonos_pos_signal.connect(self._on_sonos_pos)
        self._probe_signal.connect(self._on_probe_result)
        self._playback_end_signal.connect(self._on_playback_end)
        self.uri_label.textChanged.connect(self._on_uri_changed)

        self._sonos_timer = QTimer(self)
        self._sonos_timer.timeout.connect(self._poll_sonos_position)
        self._sonos_timer.start(250)

        try:
            vol = speaker.volume
            self.vol_slider.setValue(vol)
            self.vol_label.setText(f"Volume: {vol}")
        except (Exception,):
            pass

        layout.addWidget(content_area)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_position)
        self._poll_timer.start(500)

        self._sync_thread = threading.Thread(
            target=self._sync_monitor_loop, daemon=True, name="sonos-sync-monitor"
        )
        self._sync_thread.start()

    def _on_schedule_color_log(self):
        # Poll until video is actually loaded before logging color state
        if not self._player:
            return
        try:
            vo = self._player.current_vo
            if vo is not None:
                self._log_color_info()
                return
        except (Exception,):
            pass
        QTimer.singleShot(1000, self._on_schedule_color_log)

    def _poll_sonos_position(self):
        # QTimer tick: speed indicator only. Sync correction lives in _sync_monitor_loop.
        spd = self._current_speed
        if abs(spd - 1.0) > 0.005:
            self.speed_label.setText(f"{spd:.3f}x")
        else:
            self.speed_label.setText("")

    def _sync_monitor_loop(self):
        global seek_base_pos
        stall_streak = 0
        while True:
            time.sleep(self.SONOS_POLL_INTERVAL)
            if not self._sync_ready or self._seeking or self._seek_in_progress:
                continue
            try:
                transport = speaker.get_current_transport_info()
                state = transport.get("current_transport_state", "")
            except (Exception,):
                continue  # transient SoCo/network error — skip tick, never restart on it
            if state not in ("PLAYING", "TRANSITIONING"):
                stall_streak += 1
                if stall_streak >= self.SONOS_STALL_TICKS:
                    stall_streak = 0
                    self._restart_sonos_at_current_pos()
                continue
            stall_streak = 0
            if state != "PLAYING":
                continue  # TRANSITIONING — no correction while Sonos switches
            now = time.time()
            prev_poll = self._last_poll_time
            self._last_poll_time = now
            try:
                info = speaker.get_current_track_info()
                pos_str = info.get('position', '')
                if not pos_str or pos_str == 'NOT_IMPLEMENTED':
                    continue
                elapsed = parse_sonos_position(pos_str)
                if elapsed <= 0:
                    continue
                if elapsed != self._last_soco_elapsed:
                    # Estimate the true tick moment as the midpoint between the
                    # previous poll (old value) and this one (new value) — halves
                    # the extrapolation sawtooth vs. stamping "now".
                    if prev_poll > 0 and (now - prev_poll) <= 1.0:
                        self._last_soco_time = (prev_poll + now) / 2.0
                    else:
                        self._last_soco_time = now
                    self._last_soco_elapsed = elapsed
                stalled = (now - self._last_soco_time) > self.SONOS_STALL_SECS and now >= self._correction_holdoff_until
                if stalled and not self._stall_active:
                    self._stall_active = True
                    self._stall_started_at = now
                    print("[SYNC] Sonos stalled — holding video frame")
                elif not stalled and self._stall_active:
                    # First fresh reading after a stall: re-base cleanly.
                    self._stall_active = False
                    self._drift_hist = []
                    self._fd_samples = []
                    self._correction_holdoff_until = now + self.REANCHOR_SETTLE
                    print("[SYNC] Sonos recovered — resuming video")
                    with self._av_lock:
                        if self._player:
                            try:
                                self._player.pause = False
                            except (Exception,):
                                pass
                    continue
                if stalled:
                    if (now - self._stall_started_at) > self.SONOS_STALL_RESTART_AFTER:
                        print(f"[SYNC] Sonos stalled >{self.SONOS_STALL_RESTART_AFTER:.0f}s — forcing stream restart")
                        self._stall_active = False
                        self._last_soco_time = now  # restart at last KNOWN position, not extrapolated
                        self._restart_sonos_at_current_pos()
                        continue
                    with self._av_lock:
                        if self._player:
                            try:
                                self._player.pause = True
                            except (Exception,):
                                pass
                    continue
                time_since = min(now - self._last_soco_time, self.EXTRAP_CAP)
                audio_pos = seek_base_pos + self._last_soco_elapsed + time_since - self._user_sync_offset - self._sync_offset_runtime
                if audio_pos < 0:
                    audio_pos = 0.0
                self._sonos_pos_signal.emit(pos_str, audio_pos)
            except Exception as e:
                print(f"[SYNC] Poll error: {e}")
                continue
            if not self._player:
                continue
            try:
                video_pos = self._player.time_pos
            except (Exception,):
                continue
            if video_pos is None:
                continue
            fd = video_pos - audio_pos
            self._fd_samples.append((now, fd))
            if len(self._fd_samples) > 600:
                self._fd_samples.pop(0)
            self._learn_from_drift(now)
            self._apply_drift_correction(fd, audio_pos)

    def _restart_sonos_at_current_pos(self):
        global seek_base_pos
        if self._seek_in_progress:
            return
        audio_url = getattr(self, '_resolved_audio_url', None)
        if not (self._current_uri and self._player and audio_url):
            return
        audio_pos = seek_base_pos + self._last_soco_elapsed + min(
            time.time() - self._last_soco_time, self.EXTRAP_CAP)
        if audio_pos < 0:
            audio_pos = 0.0
        print(f"[SYNC] Sonos stopped — restarting stream at {audio_pos:.1f}s")
        set_audio_stream(audio_url, seek_sec=audio_pos,
                         headers=getattr(self, '_resolved_audio_headers', {}))
        seek_base_pos = audio_pos
        self._last_soco_elapsed = 0.0
        self._last_soco_time = time.time()
        self._initial_sync_done = False
        self._reset_sync_state()
        start_sonos_stream()

    def _apply_drift_correction(self, drift, audio_pos):
        now = time.time()
        if now < self._correction_holdoff_until:
            return  # seek/settle window — measurements taken now are unreliable
        with self._av_lock:
            if not self._player:
                return
            if not self._initial_sync_done:
                base = 1.0 - self._sync_rate_bias
                try:
                    self._player.pause = True
                    self._player.seek(audio_pos, "absolute")
                    self._player.pause = False
                    self._player.speed = base
                except Exception as e:
                    print(f"[SYNC] Init sync error: {e}")
                    return
                self._current_speed = base
                self._drift_hist = []
                self._drift_f = drift
                self._last_resync = now
                self._initial_sync_done = True
                self._wait_for_mpv_seek(audio_pos)
                self._correction_holdoff_until = time.time() + self.REANCHOR_SETTLE
                print(f"[SYNC] Init sync: drift={drift:.2f}s → {audio_pos:.1f}s")
                return
            self._drift_hist.append(drift)
            if len(self._drift_hist) > 7:
                self._drift_hist.pop(0)
            med = sorted(self._drift_hist)[len(self._drift_hist) // 2]
            self._drift_f = 0.65 * self._drift_f + 0.35 * med
            fd = self._drift_f
            reanchor_allowed = (now - self._last_resync) > self.REANCHOR_THROTTLE
            if self._play_started_at and (now - self._play_started_at) < self.PLAY_WARMUP_GRACE:
                reanchor_allowed = False  # decode warmup: let the speed corrector catch up, don't cut
            if abs(fd) > self.DRIFT_REANCHOR and reanchor_allowed:
                base = 1.0 - self._sync_rate_bias
                try:
                    self._player.pause = True
                    self._player.seek(audio_pos, "absolute")
                    self._player.pause = False
                    self._player.speed = base
                except Exception as e:
                    print(f"[SYNC] Re-anchor error: {e}")
                    return
                self._current_speed = base
                self._drift_f = 0.0
                self._drift_hist = []
                self._corr_engaged = False
                self._last_resync = now
                self._wait_for_mpv_seek(audio_pos)
                self._correction_holdoff_until = time.time() + self.REANCHOR_SETTLE
                print(f"[SYNC] Re-anchor: drift={fd:.2f}s → {audio_pos:.1f}s")
                return
            if self._corr_engaged and abs(fd) <= self.DRIFT_RELEASE:
                self._corr_engaged = False
            if not self._corr_engaged:
                if abs(fd) <= self.DRIFT_ENGAGE:
                    base = 1.0 - self._sync_rate_bias
                    if abs(self._current_speed - base) > 1e-6:
                        try:
                            self._player.speed = base
                        except (Exception,):
                            pass
                        self._current_speed = base
                    return
                self._corr_engaged = True
                self._last_engage_time = now
            base = 1.0 - self._sync_rate_bias
            if abs(fd) <= self.DRIFT_FINE:
                spd = base - fd / self.SPEED_FINE_DIV
                clamp = self.SPEED_FINE_CLAMP
            else:
                spd = base - fd / self.SPEED_COARSE_DIV
                clamp = self.SPEED_COARSE_CLAMP
            spd = max(base - clamp, min(base + clamp, spd))
            if abs(spd - self._current_speed) > 0.005 and (now - self._last_speed_write) >= self.SPEED_CHANGE_MIN_INTERVAL:
                try:
                    self._player.speed = spd
                except (Exception,):
                    pass
                self._current_speed = spd
                self._last_speed_write = now
                self._corrections_count += 1
                print(f"[SYNC] Speed: drift={fd:.3f}s → {spd:.3f}x")

    def _learn_from_drift(self, now):
        # Telemetry first — prints even while corrections are active.
        if self._fd_samples and (now - self._last_telemetry) >= self.TELEMETRY_INTERVAL:
            self._last_telemetry = now
            vals = [fd for _, fd in self._fd_samples]
            med = sorted(vals)[len(vals) // 2]
            print(f"[SYNC] drift: median={med:+.3f}s rate_bias={self._sync_rate_bias * 1e6:+.0f}ppm "
                  f"offset={self._sync_offset_runtime:+.3f}s engaged={self._corr_engaged} corr={self._corrections_count}/60s")
            self._corrections_count = 0
        # Learning only during quiet periods (no correction in the window).
        if self._last_engage_time and (now - self._last_engage_time) < self.LEARN_QUIET:
            return
        if now < self._correction_holdoff_until:
            return
        clean = [(t, fd) for (t, fd) in self._fd_samples if t >= now - self.LEARN_QUIET]
        if len(clean) < 150:
            return
        if now - clean[0][0] < self.LEARN_MIN_WINDOW:
            return
        vals = [fd for _, fd in clean]
        spread = max(vals) - min(vals)
        med = sorted(vals)[len(vals) // 2]

        # 1) Anchor offset calibration: a stable nonzero median is a constant
        #    bias (Sonos buffer/report offset), not drift — trim it out once.
        if spread <= self.OFFSET_SPREAD_MAX and abs(med) > self.OFFSET_TRIM_MIN:
            self._sync_offset_runtime -= med
            self._fd_samples = []
            print(f"[SYNC] Offset trim: {med:+.3f}s → runtime offset {-self._sync_offset_runtime:+.3f}s")
            return

        # 2) Rate feed-forward: early-vs-late window medians estimate the drift
        #    slope (Sonos clock vs PC clock). Apply as a constant base speed.
        n = len(clean)
        third = n // 3
        early = sorted(vals[:third])
        late = sorted(vals[-third:])
        m_e = early[len(early) // 2]
        m_l = late[len(late) // 2]
        t_e = clean[third // 2][0]
        t_l = clean[n - 1 - third // 2][0]
        dt = t_l - t_e
        if dt < 30.0:
            return
        slope = (m_l - m_e) / dt
        if abs(slope) * 1e6 < self.RATE_MIN_PPM:
            return
        if abs(slope) > self.RATE_MAX_PPM * 1e-6:
            print(f"[SYNC] Rate estimate out of range ({slope * 1e6:+.0f}ppm) — ignoring")
            self._fd_samples = []
            return
        self._sync_rate_bias = max(-self.RATE_BIAS_CLAMP,
                                   min(self.RATE_BIAS_CLAMP, self._sync_rate_bias + slope))
        self._fd_samples = []
        base = 1.0 - self._sync_rate_bias
        with self._av_lock:
            if self._player and abs(self._current_speed - base) > 1e-6:
                try:
                    self._player.speed = base
                except (Exception,):
                    pass
                self._current_speed = base
                self._corrections_count += 1
        print(f"[SYNC] Rate match: {slope * 1e6:+.0f}ppm → base speed {base:.5f}")

    def _on_sonos_pos(self, pos_str, audio_pos):
        if audio_pos > 0:
            self.sonos_label.setText(f"Sonos: {fmt_time(int(audio_pos*1000))}")
        else:
            self.sonos_label.setText(f"Sonos: {pos_str}")

    def _on_playback_end(self):
        if not self._player or not self._current_uri:
            return
        print(f"[PLAY] Playback reached end — stopping Sonos stream")
        self.do_stop()

    def _reset_sync_state(self):
        self._drift_f = 0.0
        self._drift_hist = []
        self._last_resync = 0.0
        self._corr_engaged = False

    def _on_uri_changed(self, text):
        if not text.strip():
            return
        try:
            self._probe_timer.stop()
        except (AttributeError, RuntimeError):
            pass
        if not hasattr(self, '_probe_timer'):
            self._probe_timer = QTimer(self)
            self._probe_timer.setSingleShot(True)
            self._probe_timer.timeout.connect(lambda: self._schedule_probe(self.uri_label.text().strip()))
        self._probe_timer.start(800)

    def _schedule_probe(self, uri):
        if not uri or uri == self._last_probed_uri:
            return
        self._last_probed_uri = uri
        self.audio_track_combo.blockSignals(True)
        self.audio_track_combo.clear()
        self.audio_track_combo.addItem("Probing...", -2)
        self.audio_track_combo.setEnabled(False)
        self.audio_track_combo.blockSignals(False)
        self._audio_tracks = []
        self._probe_data = None
        self._selected_audio_index = -1
        threading.Thread(target=self._probe_worker, args=(uri,), daemon=True).start()

    def _probe_worker(self, uri):
        result = probe_audio_tracks(uri)
        self._probe_signal.emit(result)

    def _on_probe_result(self, result):
        self.audio_track_combo.blockSignals(True)
        self.audio_track_combo.clear()
        if result is None:
            code = _last_yt_error
            self._probe_error_reason = code
            if code:
                self.audio_track_combo.addItem(_yt_error_text(code), -1)
                self.status_label.setText("Can't play: " + _yt_error_text(code))
            else:
                self.audio_track_combo.addItem("Probe failed", -1)
                self.status_label.setText("Probe failed")
            self.audio_track_combo.setEnabled(False)
            self._audio_tracks = []
            self._probe_data = None
        else:
            tracks = result.get("audio_tracks", [])
            self._probe_data = result
            self._probe_error_reason = None
            self._audio_tracks = tracks
            if tracks:
                for i, t in enumerate(tracks):
                    display = t.get("title", f"Stream {t['index']}")
                    self.audio_track_combo.addItem(display, i)
                self.audio_track_combo.setEnabled(True)
            else:
                self.audio_track_combo.addItem("No audio tracks found", -1)
                self.audio_track_combo.setEnabled(False)
        self.audio_track_combo.setCurrentIndex(0)
        self.audio_track_combo.blockSignals(False)

    def _on_audio_track_changed(self, index):
        idx = self.audio_track_combo.itemData(index)
        self._selected_audio_index = idx if idx is not None else -1
        global selected_audio_track
        if self._probe_data and self._probe_data.get("type") == "local":
            selected_audio_track = self._selected_audio_index
        else:
            selected_audio_track = -1
        print(f"[AUDIO] Track selected: index={self._selected_audio_index}, global={selected_audio_track}")
        # Apply immediately when a local file is actively playing:
        # re-spawn the audio stream at the current position with the new track.
        if (self._probe_data and self._probe_data.get("type") == "local"
                and self._current_uri and self._player
                and not self._seek_in_progress
                and self._duration_ms > 0):
            pos_ms, _ = self.get_position()
            if pos_ms > 0:
                print(f"[AUDIO] Applying track change at {pos_ms / 1000.0:.1f}s")
                self._do_seek(pos_ms)

    def _check_subtitle_tracks(self):
        """Poll mpv track_list and update subtitle combo if tracks changed."""
        if not self._player:
            return
        try:
            track_list = self._player.track_list
        except (Exception,):
            return
        if not track_list:
            return
        subs = [t for t in track_list if t.get("type") == "sub"]
        new_ids = tuple(t.get("id", 0) for t in subs)
        if new_ids == self._sub_last_track_ids:
            self._sync_sub_selection()
            return
        self._sub_last_track_ids = new_ids
        self._subtitle_tracks = subs
        self.sub_combo.blockSignals(True)
        self.sub_combo.clear()
        if not subs:
            self.sub_combo.addItem("No subtitles", -1)
            self.sub_combo.setEnabled(False)
        else:
            self.sub_combo.addItem("Off", 0)
            for t in subs:
                lang = (t.get("lang") or t.get("title") or "").upper() or f"Track {t['id']}"
                codec = t.get("codec_name", t.get("codec", ""))
                label = f"{lang} ({codec})" if codec else lang
                self.sub_combo.addItem(label, t["id"])
            self.sub_combo.setEnabled(True)
        self._sync_sub_selection()
        self.sub_combo.blockSignals(False)

    def _sync_sub_selection(self):
        """Sync combo selection and toggle button to current mpv subtitle state."""
        if not self._player:
            return
        try:
            current_sid = self._player.sid or 0
        except (Exception,):
            current_sid = 0
        idx = self.sub_combo.findData(current_sid)
        if idx >= 0 and idx != self.sub_combo.currentIndex():
            self.sub_combo.blockSignals(True)
            self.sub_combo.setCurrentIndex(idx)
            self.sub_combo.blockSignals(False)
        try:
            vis = self._player.sub_visibility
            if vis != self.sub_btn.isChecked():
                self.sub_btn.blockSignals(True)
                self.sub_btn.setChecked(vis)
                self.sub_btn.blockSignals(False)
        except (Exception,):
            pass

    def _on_sub_changed(self, index):
        tid = self.sub_combo.itemData(index)
        if tid is None or not self._player:
            return
        try:
            self._player.sid = tid
            print(f"[SUB] Track set to sid={tid}")
        except Exception as e:
            print(f"[SUB] Error setting sid: {e}")

    def _on_sub_toggle(self, checked):
        if not self._player:
            return
        try:
            self._player.sub_visibility = checked
            self.sub_btn.setText("Subs: ON" if checked else "Subs: OFF")
            print(f"[SUB] Visibility: {'ON' if checked else 'OFF'}")
        except Exception as e:
            print(f"[SUB] Error toggling visibility: {e}")

    def _color_output_kwargs(self):
        """Return mpv options for the current color/output configuration.

        The HDR toggle describes the DISPLAY, not the source. The detected
        source only decides how gamut/tone mapping is handled.
        """
        if not self._hdr_enabled:
            return {
                "target_prim": "auto",
                "target_trc": "auto",
                "target_peak": "auto",
                "gamut_mapping_mode": "auto",
                "target_colorspace_hint": "no",
                "inverse_tone_mapping": "no",
                "hdr_compute_peak": "auto",
            }
        kwargs = {
            "target_prim": "bt.2020",
            "target_trc": "pq",
            "target_peak": self._nits,
            "target_colorspace_hint": "yes",
            "target_colorspace_hint_mode": "target",
            "inverse_tone_mapping": "no",
            "hdr_compute_peak": "auto",
        }
        if self._source_is_hdr:
            kwargs["gamut_mapping_mode"] = "clip"
        else:
            kwargs["gamut_mapping_mode"] = "auto"
            kwargs["inverse_tone_mapping"] = "yes"
            kwargs["hdr_compute_peak"] = "no"
        return kwargs

    def _configure_color_output(self):
        """Apply the current color/output configuration to the player."""
        if not self._player:
            return
        try:
            for key, value in self._color_output_kwargs().items():
                setattr(self._player, key, value)
            if self._hdr_enabled:
                src = "HDR" if self._source_is_hdr else "SDR (upconverted)"
                print(f"[COLOR] Target = HDR, {self._nits} nits (src={src})")
            else:
                print("[COLOR] Target = SDR")
        except Exception as e:
            print(f"[HDR] Apply error: {e}")

    def _ensure_player(self):
        if self._player is not None:
            return True
        try:
            kwargs = dict(
                geometry="1280x720",
                autofit="1280x720",
                title="Sonos PC Streamer - Video",
                pause=True,
                vo="gpu-next",
                gpu_api="d3d11",
                hwdec="auto",
                ytdl=True,
                ytdl_format="bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestaudio/best",
                ao="null",
            )
            kwargs.update(self._color_output_kwargs())
            self._player = _mpv.MPV(**kwargs)
            if self._hdr_enabled:
                src = "HDR" if self._source_is_hdr else "SDR (upconverted)"
                print(f"[MPV] Player created with HDR output {self._nits}nits (src={src})")
            else:
                print("[MPV] Player created with SDR output")
            print("[MPV] Player created via libmpv")
            @self._player.event_callback("end-file")
            def _end_file_handler(event):
                try:
                    data = event.as_dict()
                    if data.get("reason") == "eof":
                        self._playback_end_signal.emit()
                except Exception as ex:
                    print(f"[MPV] end-file callback error: {ex}")
            return True
        except Exception as e:
            print(f"[MPV] Failed to create player: {e}")
            self.status_label.setText(f"mpv error: {e}")
            return False

    def _persist_settings(self):
        save_settings({"hdr_enabled": self._hdr_enabled, "nits": self._nits,
                       "sync_offset_seconds": self._user_sync_offset,
                       "last_open_dir": self._last_open_dir})

    def _toggle_hdr(self, checked):
        self._hdr_enabled = checked
        self.hdr_btn.setText("HDR: ON" if checked else "HDR: OFF")
        self.nits_spin.setEnabled(checked)
        self._persist_settings()
        self._configure_color_output()
        print(f"[HDR] Toggle: {'ON' if checked else 'OFF'}")

    def _on_nits_changed(self, value):
        self._nits = value
        self._persist_settings()
        self._configure_color_output()
        print(f"[HDR] Nits set to {value}")

    def _on_sync_nudge(self, delta):
        self._user_sync_offset = max(-1.0, min(1.0, self._user_sync_offset + delta))
        self.sync_offset_label.setText(f"Video Sync: {self._user_sync_offset:+.2f}s")
        self._persist_settings()
        if delta > 0:
            print(f"[SYNC] {self._user_sync_offset:+.2f}s - video delayed by {abs(self._user_sync_offset):.2f}s")
        else:
            print(f"[SYNC] {self._user_sync_offset:+.2f}s - video advanced by {abs(self._user_sync_offset):.2f}s")

    def _on_sync_nudge_plus(self):
        self._on_sync_nudge(0.05)

    def _on_sync_nudge_minus(self):
        self._on_sync_nudge(-0.05)

    def _log_color_info(self):
        """Log actual color state after content is loaded."""
        if not self._player:
            return
        parts = []
        for name in ("current_vo", "target_colorspace_hint",
                      "target_colorspace_hint_mode", "target_prim",
                      "target_trc", "target_peak", "gamut_mapping_mode",
                      "inverse_tone_mapping", "hdr_compute_peak",
                      "colormatrix", "colorlevels",
                      "hwdec_current"):
            try:
                val = getattr(self._player, name)
                parts.append(f"{name}={val}")
            except (Exception,):
                parts.append(f"{name}=n/a")
        for name in ("video-params/primaries", "video-params/gamma",
                     "video-params/sig-peak", "video-params/light",
                     "video-output-params/primaries", "video-output-params/gamma",
                     "video-output-params/sig-peak", "video-output-params/light"):
            try:
                val = self._player._get_property(name)
                parts.append(f"{name}={val}")
            except (Exception,):
                parts.append(f"{name}=n/a")
        parts.append(f"source_hdr={getattr(self, '_source_is_hdr', None)}")
        print(f"[COLOR] {' '.join(parts)}")

    def get_position(self):
        if not self._player:
            return 0, 0
        try:
            pos = self._player.time_pos
            dur = self._player.duration
            pos_ms = int(pos * 1000) if pos is not None else 0
            dur_ms = int(dur * 1000) if dur is not None else 0
            return pos_ms, dur_ms
        except (Exception,):
            return 0, 0

    def _poll_position(self):
        if not self._player:
            return
        self._check_subtitle_tracks()
        if getattr(self, '_is_live', False):
            self.time_label.setText("LIVE / --:--")
            return
        pos_ms, dur_ms = self.get_position()
        if dur_ms > 0:
            self._duration_ms = dur_ms
            self.seek_slider.blockSignals(True)
            self.seek_slider.setRange(0, dur_ms)
            self.seek_slider.blockSignals(False)
        if pos_ms > 0 and not self._seeking:
            self.seek_slider.blockSignals(True)
            self.seek_slider.setValue(pos_ms)
            self.seek_slider.blockSignals(False)
            self.time_label.setText(f"{fmt_time(pos_ms)} / {fmt_time(self._duration_ms)}")

    def _on_seek_pressed(self):
        self._seeking = True

    def _on_seek_moved(self, value):
        self.time_label.setText(f"{fmt_time(value)} / {fmt_time(self._duration_ms)}")

    def _on_seek_released(self):
        self._seeking = False
        pos_ms = self.seek_slider.value()
        self._do_seek(pos_ms)

    def _wait_for_mpv_seek(self, target, timeout=6.0):
        """Block until mpv's async seek lands near target (mpv is paused during seeks)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                tp = self._player.time_pos if self._player else None
            except (Exception,):
                tp = None
            if tp is not None and abs(tp - target) <= 0.3:
                return
            time.sleep(0.15)
        print(f"[SYNC] mpv seek did not settle in time (target={target:.1f}s) — monitor will re-check")

    def _do_seek(self, pos_ms):
        if getattr(self, '_is_live', False):
            return
        pos_sec = pos_ms / 1000.0
        self._seek_gen += 1
        gen = self._seek_gen
        self._seek_in_progress = True
        if self._player:
            with self._av_lock:
                try:
                    self._player.pause = True
                    self._player.seek(pos_sec, "absolute")
                    self._player.speed = 1.0
                    self._current_speed = 1.0
                except Exception as e:
                    print(f"[MPV] Seek error: {e}")
        if not self._current_uri:
            self._seek_in_progress = False
            return

        audio = self._resolved_audio_url

        def _seek_success():
            global seek_base_pos
            seek_base_pos = pos_sec
            self._sync_ready = True
            self._initial_sync_done = True
            self._reset_sync_state()
            self._last_soco_elapsed = 0.0
            self._last_soco_time = time.time()
            self._fd_samples = []
            self._sync_offset_runtime = 0.0
            self._stall_active = False
            self._stall_started_at = 0.0
            self._correction_holdoff_until = time.time() + 2.0
            if self._player:
                with self._av_lock:
                    try:
                        base = 1.0 - self._sync_rate_bias
                        self._player.pause = False
                        self._player.speed = base
                        self._current_speed = base
                    except Exception as ex:
                        print(f"[SYNC] Seek resume error: {ex}")
            print(f"[SEEK] Done: base={pos_sec:.1f}s, monitor will fine-tune")

        def _seek_fail(message):
            # Keep base/offset consistent so the monitor's auto-restart (todo C)
            # resumes at the right spot and re-anchors when Sonos revives.
            global seek_base_pos
            seek_base_pos = pos_sec
            self._sync_ready = True
            self._initial_sync_done = False
            self._reset_sync_state()
            self._last_soco_elapsed = 0.0
            self._last_soco_time = time.time()
            self._correction_holdoff_until = time.time() + 3.0
            if self._player:
                with self._av_lock:
                    try:
                        self._player.pause = False
                    except (Exception,):
                        pass
            self._status_signal.emit(message)
            print(f"[SEEK] Failed: {message}")

        def work():
            global _stream_output_seek_only
            try:
                if self._seek_gen != gen:
                    return
                if not audio:
                    print("[SEEK] No resolved audio URL, video-only seek")
                    self._sync_ready = True
                    return
                headers = getattr(self, '_resolved_audio_headers', None)
                # Land the video seek FIRST so Sonos doesn't get a head start
                # while mpv is still decoding into the seek target.
                self._wait_for_mpv_seek(pos_sec)
                if self._seek_gen != gen:
                    return
                set_audio_stream(audio, seek_sec=pos_sec, headers=headers)
                if self._seek_gen != gen:
                    return
                start_sonos_stream()
                status = wait_for_sonos_audio()
                if self._seek_gen != gen:
                    return
                if status != "ok" and audio.startswith("http") and not _stream_output_seek_only:
                    err_tail = " ".join(_ffmpeg_stderr_tail)
                    if "403" in err_tail or "Forbidden" in err_tail or "HTTP error" in err_tail:
                        print("[SEEK] ffmpeg HTTP fetch error — output-seek fallback skipped")
                    else:
                        print("[SEEK] No audio via input seek — retrying once with output seek")
                        _stream_output_seek_only = True
                        try:
                            set_audio_stream(audio, seek_sec=pos_sec, headers=headers)
                            if self._seek_gen != gen:
                                return
                            start_sonos_stream()
                            status = wait_for_sonos_audio()
                            if self._seek_gen != gen:
                                return
                        finally:
                            _stream_output_seek_only = False
                if status == "ok":
                    _seek_success()
                else:
                    _seek_fail("Seek: audio stream unavailable — video continues")
            finally:
                if self._seek_gen == gen:
                    self._seek_in_progress = False

        threading.Thread(target=work, daemon=True).start()

    def browse_file(self):
        start_dir = self._last_open_dir if os.path.isdir(self._last_open_dir) else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Media File", start_dir,
            "Media Files (*.mp4 *.mkv *.avi *.mov *.ts *.m4v *.webm *.flv *.mp3 *.flac *.wav *.ogg);;All Files (*)",
        )
        if path:
            self.uri_label.setText(path)
            self._last_open_dir = os.path.dirname(path)
            self._persist_settings()

    def on_play(self):
        uri = self.uri_label.text().strip()
        if not uri:
            return
        self.do_play_uri(uri)

    def do_play_uri(self, uri):
        global audio_uri, audio_seek_offset, sonos_playing, seek_base_pos
        self.do_stop()
        self._current_uri = uri
        self._is_live = False
        self._resolved_audio_url = None
        audio_uri = uri
        audio_seek_offset = 0
        sonos_playing = False
        self.status_label.setText("Starting...")

        if not self._ensure_player():
            return

        self.status_label.setText("Resolving URL...")
        print(f"[PLAY] Resolving: {uri}")

        def _resolve_and_play_inner():
            global seek_base_pos

            # Defensive init
            video_headers = {}

            # Check if we have cached probe data from pre-play probing
            use_cache = (self._probe_data is not None
                         and self._last_probed_uri == uri
                         and self._probe_data.get("audio_tracks"))

            if use_cache and self._probe_data["type"] == "url":
                # Use cached probe data (avoids redundant yt-dlp call)
                data = self._probe_data
                _video_url = data.get("video_url", uri)
                video_headers = data.get("video_headers", {})
                title = data.get("title", uri)
                duration = data.get("duration", 0) or 0
                is_live = data.get("is_live", False) or False
                tracks = data.get("audio_tracks", [])
                sel = self._selected_audio_index
                if 0 <= sel < len(tracks):
                    audio_url = tracks[sel].get("url", uri)
                    audio_headers = tracks[sel].get("http_headers", {})
                elif tracks:
                    audio_url = tracks[0].get("url", uri)
                    audio_headers = tracks[0].get("http_headers", {})
                else:
                    audio_url = uri
                    audio_headers = {}
                print(f"[PLAY] Using cached probe data: {title}")
            elif use_cache and self._probe_data["type"] == "local":
                # Local file: use raw URI; track index from selected_audio_track
                _video_url = uri
                audio_url = uri
                title = os.path.basename(uri)
                duration = 0
                is_live = False
                audio_headers = {}
                print(f"[PLAY] Local file with cached probe: {title}")
            else:
                _video_url, audio_url, title, duration, is_live, video_headers, audio_headers = resolve_media_url(uri)

                if audio_url is None:
                    code = _last_yt_error
                    if code is not None:
                        print(f"[PLAY] Resolve blocked: {_yt_error_text(code)}")
                        self._status_signal.emit("Can't play: " + _yt_error_text(code))
                        return

                # Step 2: If yt-dlp failed, scrape the page HTML directly
                if audio_url is None:
                    print(f"[PLAY] yt-dlp failed, scraping page HTML...")
                    scraped = scrape_page_for_media(uri)
                    if scraped:
                        audio_url = scraped
                        print(f"[PLAY] Scrape found: {audio_url[:120]}")

                # Step 3: Fallback to raw URI
                if audio_url is None:
                    audio_url = uri
                    title = title or uri
                    is_live = False
                    print(f"[PLAY] Could not resolve media, using raw URI")
                else:
                    print(f"[PLAY] Final audio URL: {audio_url[:120]}...")

            self._resolved_audio_url = audio_url
            self._resolved_audio_headers = audio_headers
            self._is_live = is_live

            # Detect source HDR and apply color settings
            self._source_is_hdr = False
            if self._probe_data:
                self._source_is_hdr = self._probe_data.get("is_hdr", False)
            print(f"[PLAY] Source HDR: {self._source_is_hdr}, HDR toggle: {self._hdr_enabled}")
            self._configure_color_output()

            if is_live:
                self.seek_slider.setEnabled(False)
                self.time_label.setText("LIVE / --:--")
                print(f"[PLAY] Live stream detected: {title}")
            else:
                self.seek_slider.setEnabled(True)

            try:
                self._player.pause = True
                self._player.speed = 1.0
                self._current_speed = 1.0
                try:
                    self._player.ytdl_format = (
                        "bestvideo+bestaudio/best"
                        if self._source_is_hdr
                        else "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestaudio/best"
                    )
                except (Exception,):
                    pass
                direct_video = (isinstance(_video_url, str) and _video_url.startswith("http")
                                and _video_url != uri)
                if direct_video:
                    try:
                        hdr_fields = [f"{k}: {v}" for k, v in (video_headers or {}).items()]
                        if not any(k.lower() == "user-agent" for k in (video_headers or {})):
                            hdr_fields.append(
                                "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
                        self._player._set_property("http-header-fields", hdr_fields)
                        print(f"[PLAY] Direct video URL mode ({len(hdr_fields)} header fields)")
                    except Exception as e:
                        print(f"[PLAY] Could not set video headers: {e}")
                    self._player.play(_video_url)
                    threading.Thread(target=self._video_load_fallback, args=(uri,), daemon=True).start()
                else:
                    self._player.play(uri)
                print(f"[MPV] Playing: {title}")
                self._schedule_color_log.emit()
            except Exception as e:
                print(f"[MPV] Play error: {e}")
                self._status_signal.emit(f"Error: {e}")
                return

            set_audio_stream(audio_url, seek_sec=0, headers=self._resolved_audio_headers)
            start_sonos_stream()
            status = wait_for_sonos_audio()

            if status != "ok" and not is_live and audio_url.startswith("http"):
                print("[PLAY] Audio stream failed — re-resolving and retrying once")
                _v2, audio_url2, _t2, _d2, _live2, _vh2, audio_headers2 = resolve_media_url(uri)
                if audio_url2:
                    self._resolved_audio_url = audio_url2
                    self._resolved_audio_headers = audio_headers2
                    audio_url = audio_url2
                    set_audio_stream(audio_url2, seek_sec=0, headers=audio_headers2)
                    start_sonos_stream()
                    status = wait_for_sonos_audio()

            if is_live:
                self._play_started_at = time.time()
                seek_base_pos = 0.0
                self._sync_ready = True
                self._initial_sync_done = True
                self._reset_sync_state()
                self._current_speed = 1.0
                self._status_signal.emit(f"Playing LIVE: {title}")
                if self._player:
                    try:
                        self._player.pause = False
                        self._player.speed = 1.0
                    except (Exception,):
                        pass
            elif status == "ok":
                self._play_started_at = time.time()
                seek_base_pos = 0.0
                self._sync_ready = True
                print(f"[SYNC] Initial: base=0, monitor will sync")
                self._status_signal.emit(f"Playing: {title}")
            else:
                if self._player:
                    try:
                        self._player.pause = False
                    except (Exception,):
                        pass
                self._status_signal.emit(f"Playing (video only): {title}")

        def resolve_and_play():
            global seek_base_pos
            try:
                _resolve_and_play_inner()
            except Exception as e:
                traceback.print_exc()
                self._status_signal.emit(f"Error: {e}")

        threading.Thread(target=resolve_and_play, daemon=True).start()

    def _video_load_fallback(self, page_uri):
        """If the direct video URL never decodes, fall back to page-URL playback."""
        deadline = time.time() + 15.0
        while time.time() < deadline:
            time.sleep(0.5)
            if self._current_uri != page_uri:
                return  # playback changed — not our concern anymore
            try:
                if self._player and self._player._get_property("video-params/w") is not None:
                    return  # direct URL is decoding fine
            except (Exception,):
                pass
        if self._current_uri != page_uri or not self._player:
            return
        try:
            print("[PLAY] Direct video URL failed to load — falling back to page playback")
            self._player.play(page_uri)
        except Exception as e:
            print(f"[PLAY] Fallback playback error: {e}")

    def do_stop(self):
        global audio_uri, audio_seek_offset, ffmpeg_process, seek_base_pos, _stream_output_seek_only
        self._seek_gen += 1
        self._seek_in_progress = False
        _stream_output_seek_only = False
        seek_base_pos = 0.0
        self._current_speed = 1.0
        self._sync_ready = False
        self._initial_sync_done = False
        self._reset_sync_state()
        self._last_soco_elapsed = 0.0
        self._last_soco_time = 0.0
        self._stall_active = False
        self._stall_started_at = 0.0
        audio_uri = None
        audio_seek_offset = 0
        self._current_uri = None
        self._is_live = False
        self._source_is_hdr = False
        self._resolved_audio_url = None
        self._resolved_audio_headers = {}
        self._subtitle_tracks = []
        self._sub_last_track_ids = ()
        self.sub_combo.blockSignals(True)
        self.sub_combo.clear()
        self.sub_combo.addItem("No subtitles", -1)
        self.sub_combo.setEnabled(False)
        self.sub_combo.blockSignals(False)
        self.sub_btn.blockSignals(True)
        self.sub_btn.setChecked(True)
        self.sub_btn.setText("Subs: ON")
        self.sub_btn.blockSignals(False)
        self.seek_slider.setEnabled(True)
        with ffmpeg_lock:
            if ffmpeg_process:
                ffmpeg_process.kill()
        if self._player:
            try:
                self._player.stop()
            except (Exception,):
                pass
        stop_sonos_stream()
        self.status_label.setText("Stopped")
        self.time_label.setText("00:00 / 00:00")
        self.seek_slider.setValue(0)

    def on_stop(self):
        self.do_stop()

    def on_fullscreen(self):
        if self._player:
            try:
                self._player.fullscreen = not self._player.fullscreen
            except Exception as e:
                print(f"[MPV] Fullscreen error: {e}")

    def on_volume_changed(self, val):
        self.vol_label.setText(f"Volume: {val}")
        def set_speaker_vol():
            try:
                speaker.volume = val
            except (Exception,):
                pass
        threading.Thread(target=set_speaker_vol, daemon=True).start()

    def _edge_hit(self, pos):
        rect = self.rect()
        m = self._RESIZE_MARGIN
        x, y = pos.x(), pos.y()
        edge = 0
        if x < m:
            edge |= Qt.Edge.LeftEdge
        elif x > rect.width() - m:
            edge |= Qt.Edge.RightEdge
        if y < m:
            edge |= Qt.Edge.TopEdge
        elif y > rect.height() - m:
            edge |= Qt.Edge.BottomEdge
        return edge

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self._edge_hit(event.position().toPoint())
            if edge:
                self._resize_edge = edge
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
            elif event.position().toPoint().y() < 36:
                self._dragging = True
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
            else:
                self._resize_edge = None

    def mouseMoveEvent(self, event):
        if getattr(self, '_dragging', False) and self._drag_pos:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        if self._resize_edge:
            diff = event.globalPosition().toPoint() - self._drag_pos
            geom = self.frameGeometry()
            new_pos = geom.topLeft()

            if self._resize_edge & Qt.Edge.LeftEdge:
                new_rect = QRect(diff.x(), geom.y(), geom.width() - diff.x() + geom.x(), geom.height())
                if new_rect.width() >= self.minimumWidth():
                    new_pos.setX(diff.x())
                    geom.setWidth(new_rect.width())
            if self._resize_edge & Qt.Edge.TopEdge:
                new_rect = QRect(geom.x(), diff.y(), geom.width(), geom.height() - diff.y() + geom.y())
                if new_rect.height() >= self.minimumHeight():
                    new_pos.setY(diff.y())
                    geom.setHeight(new_rect.height())
            if self._resize_edge & Qt.Edge.RightEdge:
                geom.setWidth(max(self.minimumWidth(), diff.x() - geom.x() + 1))
            if self._resize_edge & Qt.Edge.BottomEdge:
                geom.setHeight(max(self.minimumHeight(), diff.y() - geom.y() + 1))

            self.setGeometry(geom)
            event.accept()
        else:
            edge = self._edge_hit(event.position().toPoint())
            if edge & Qt.Edge.LeftEdge or edge & Qt.Edge.RightEdge:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
            elif edge & Qt.Edge.TopEdge or edge & Qt.Edge.BottomEdge:
                self.setCursor(Qt.CursorShape.SizeVerCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        self._resize_edge = None
        self._drag_pos = None
        self._dragging = False
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def closeEvent(self, event):
        if self._player:
            try:
                self._player.terminate()
            except (Exception,):
                pass
        stop_sonos_stream()
        event.accept()


if __name__ == "__main__":
    print("Stream: http://" + PC_IP + ":" + str(stream_port) + "/stream.mp3")
    print("Sonos speaker: " + str(sonos_ip))
    print("Sonos sync offset: " + str(SYNC_OFFSET_SECONDS) + "s")

    threading.Thread(target=start_stream_server, daemon=True).start()

    qt_app = QApplication(sys.argv)
    qt_app.setStyleSheet(DARK_STYLE)
    main_window = MainWindow()
    main_window.show()
    main_window.adjustSize()

    sys.exit(qt_app.exec())
