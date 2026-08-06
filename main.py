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
import urllib.request
from urllib.parse import urlparse, urljoin

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
from PyQt5.QtCore import Qt, QTimer, QRect, pyqtSignal
from PyQt5.QtWidgets import (
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
except Exception:
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
                except Exception:
                    pass
                return True
    except Exception:
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


def wait_for_sonos_playing(timeout=60.0):
    global sonos_playing
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = speaker.get_current_transport_info()
            state = info.get("current_transport_state", "")
            if state == "PLAYING":
                sonos_playing = True
                print("[SYNC] Sonos confirmed PLAYING")
                return True
        except Exception:
            pass
        time.sleep(0.5)
    print("[SYNC] Sonos did not start playing in time")
    return False


_settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

def load_settings():
    try:
        with open(_settings_file) as f:
            return json.load(f)
    except Exception:
        return {}

def save_settings(data):
    try:
        with open(_settings_file, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[SETTINGS] Failed to save: {e}")


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
    except Exception:
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

        headers_arg = None
        if audio_uri.startswith("http") and audio_headers:
            headers_arg = "".join(f"{k}: {v}\r\n" for k, v in audio_headers.items())
        cmd = [FFMPEG_PATH, "-re"]
        if headers_arg:
            cmd += ["-headers", headers_arg]
        if audio_seek_offset > 0 and not audio_uri.startswith("http"):
            cmd += ["-ss", str(audio_seek_offset)]      # input seek for local
        cmd += ["-i", audio_uri]
        if audio_seek_offset > 0 and audio_uri.startswith("http"):
            cmd += ["-ss", str(audio_seek_offset)]      # output seek for URL
        if selected_audio_track >= 0 and not audio_uri.startswith("http"):
            cmd += ["-map", f"0:a:{selected_audio_track}"]
            print(f"[STREAM] Mapping audio stream: 0:a:{selected_audio_track}")
        cmd += ["-vn", "-acodec", "libmp3lame", "-ab", "192k",
                "-ac", "2", "-ar", "44100", "-f", "mp3", "pipe:1"]
        print(f"[STREAM] Starting ffmpeg: {audio_uri} (seek_offset={audio_seek_offset}s)")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
            )
        except Exception as e:
            print("[STREAM] Failed to start ffmpeg:", e)
            return

        with ffmpeg_lock:
            ffmpeg_process = proc

        try:
            while True:
                chunk = proc.stdout.read(1024)
                if not chunk:
                    break
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
    except Exception:
        pass


def set_audio_stream(uri, seek_sec=0, headers=None):
    global audio_uri, audio_seek_offset, audio_headers
    audio_uri = uri
    audio_seek_offset = seek_sec
    audio_headers = headers or {}
    print(f"[AUDIO] Stream offset set to {seek_sec}s")


def resolve_media_url(url):
    """Resolve a media URL to direct video + audio CDN URLs.

    Returns (video_url, audio_url, title, duration, is_live, video_headers, audio_headers) or
    (None, None, None, 0, False, {}, {}) on failure.
    """
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
                for fmt in info["formats"]:
                    if fmt.get("acodec", "none") != "none" and fmt.get("vcodec", "none") == "none":
                        best_audio = fmt
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
        except Exception:
            pass
        return {"type": "local", "audio_tracks": tracks, "is_hdr": source_hdr}
    except Exception as e:
        print(f"[PROBE] Local probe failed: {e}")
        return None


def _probe_url_audio_tracks(url):
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
                    display = " ".join(parts)

                    audio_tracks.append({
                        "index": idx,
                        "format_id": fmt.get("format_id", ""),
                        "title": display,
                        "codec": acodec,
                        "channels": "",
                        "language": fmt.get("language", ""),
                        "url": fmt_url,
                        "http_headers": fmt.get("http_headers", {}),
                    })
                    idx += 1

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
    _status_signal = pyqtSignal(str)
    _schedule_color_log = pyqtSignal()
    _sonos_pos_signal = pyqtSignal(str, float)
    _probe_signal = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sonos PC Streamer")
        self.setWindowFlags(Qt.FramelessWindowHint)
        self.setMinimumSize(720, 200)
        self.setAttribute(Qt.WA_TranslucentBackground, False)

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
        self._current_speed = 1.0
        self._last_soco_elapsed = 0.0
        self._last_soco_time = 0.0
        self._audio_tracks = []
        self._probe_data = None
        self._last_probed_uri = ""
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
        self.seek_slider = QSlider(Qt.Horizontal)
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
        self.vol_slider = QSlider(Qt.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(30)
        self.vol_slider.valueChanged.connect(self.on_volume_changed)
        vol_row.addWidget(self.vol_slider, stretch=1)
        content_layout.addLayout(vol_row)

        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignCenter)
        self.status_label.setStyleSheet("color: #555; font-size: 12px; margin-top: 4px;")
        content_layout.addWidget(self.status_label)
        self._status_signal.connect(self.status_label.setText)
        self._schedule_color_log.connect(self._on_schedule_color_log)
        self._sonos_pos_signal.connect(self._on_sonos_pos)
        self._probe_signal.connect(self._on_probe_result)
        self.uri_label.textChanged.connect(self._on_uri_changed)

        self._sonos_timer = QTimer(self)
        self._sonos_timer.timeout.connect(self._poll_sonos_position)
        self._sonos_timer.start(250)

        try:
            vol = speaker.volume
            self.vol_slider.setValue(vol)
            self.vol_label.setText(f"Volume: {vol}")
        except Exception:
            pass

        layout.addWidget(content_area)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_position)
        self._poll_timer.start(500)

    def _on_schedule_color_log(self):
        # Poll until video is actually loaded before logging color state
        if not self._player:
            return
        try:
            vo = self._player.current_vo
            if vo is not None:
                self._log_color_info()
                return
        except Exception:
            pass
        QTimer.singleShot(1000, self._on_schedule_color_log)

    def _poll_sonos_position(self):
        spd = self._current_speed
        if spd != 1.0:
            self.speed_label.setText(f"{spd:.3f}x")
        else:
            self.speed_label.setText("")
        if not self._sync_ready or self._seeking or getattr(self, '_sonos_busy', False):
            return
        self._sonos_busy = True
        threading.Thread(target=self._fetch_sonos_pos, daemon=True).start()

    def _fetch_sonos_pos(self):
        global seek_base_pos
        try:
            # Check if Sonos is still playing — auto-restart if it dropped
            try:
                transport = speaker.get_current_transport_info()
                transport_state = transport.get("current_transport_state", "")
                if transport_state not in ("PLAYING", "TRANSITIONING"):
                    if self._current_uri and self._player:
                        print(f"[SYNC] Sonos stopped ({transport_state}), restarting stream...")
                        start_sonos_stream()
                        self._initial_sync_done = False
                        return
            except Exception:
                pass

            info = speaker.get_current_track_info()
            pos_str = info.get('position', '')
            if not pos_str or pos_str == 'NOT_IMPLEMENTED':
                return
            elapsed = parse_sonos_position(pos_str)
            if elapsed <= 0:
                return
            if elapsed != self._last_soco_elapsed:
                self._last_soco_elapsed = elapsed
                self._last_soco_time = time.time()
            time_since = time.time() - self._last_soco_time
            estimated_elapsed = self._last_soco_elapsed + time_since
            audio_pos = seek_base_pos + estimated_elapsed
            self._sonos_pos_signal.emit(pos_str, audio_pos)

            if not self._player:
                return
            video_pos = self._player.time_pos
            if video_pos is None:
                return
            drift = video_pos - audio_pos

            if not self._initial_sync_done:
                self._player.pause = True
                self._player.seek(audio_pos, "absolute")
                self._player.pause = False
                self._player.speed = 1.0
                self._current_speed = 1.0
                print(f"[SYNC] Init sync: drift={drift:.1f}s → {audio_pos:.1f}s")
                self._initial_sync_done = True
            elif abs(drift) > 0.5:
                spd = 1.0 - drift / 3.0
                spd = max(0.5, min(2.0, spd))
                self._player.speed = spd
                self._current_speed = spd
                print(f"[SYNC] Speed: drift={drift:.1f}s → {spd:.3f}x")
            elif self._current_speed != 1.0:
                self._player.speed = 1.0
                self._current_speed = 1.0
        except Exception as e:
            print(f"[SYNC] Poll error: {e}")
        finally:
            self._sonos_busy = False

    def _on_sonos_pos(self, pos_str, audio_pos):
        if audio_pos > 0:
            self.sonos_label.setText(f"Sonos: {fmt_time(int(audio_pos*1000))}")
        else:
            self.sonos_label.setText(f"Sonos: {pos_str}")

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
            self.audio_track_combo.addItem("Probe failed", -1)
            self.audio_track_combo.setEnabled(False)
            self._audio_tracks = []
            self._probe_data = None
        else:
            tracks = result.get("audio_tracks", [])
            self._probe_data = result
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

    def _check_subtitle_tracks(self):
        """Poll mpv track_list and update subtitle combo if tracks changed."""
        if not self._player:
            return
        try:
            track_list = self._player.track_list
        except Exception:
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
        except Exception:
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
        except Exception:
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
            return True
        except Exception as e:
            print(f"[MPV] Failed to create player: {e}")
            self.status_label.setText(f"mpv error: {e}")
            return False

    def _toggle_hdr(self, checked):
        self._hdr_enabled = checked
        self.hdr_btn.setText("HDR: ON" if checked else "HDR: OFF")
        self.nits_spin.setEnabled(checked)
        save_settings({"hdr_enabled": checked, "nits": self._nits})
        self._configure_color_output()
        print(f"[HDR] Toggle: {'ON' if checked else 'OFF'}")

    def _on_nits_changed(self, value):
        self._nits = value
        save_settings({"hdr_enabled": self._hdr_enabled, "nits": self._nits})
        self._configure_color_output()
        print(f"[HDR] Nits set to {value}")

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
            except Exception:
                parts.append(f"{name}=n/a")
        for name in ("video-params/primaries", "video-params/gamma",
                     "video-params/sig-peak", "video-params/light",
                     "video-output-params/primaries", "video-output-params/gamma",
                     "video-output-params/sig-peak", "video-output-params/light"):
            try:
                val = self._player._get_property(name)
                parts.append(f"{name}={val}")
            except Exception:
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
        except Exception:
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

    def _do_seek(self, pos_ms):
        global seek_base_pos
        if getattr(self, '_is_live', False):
            return
        pos_sec = pos_ms / 1000.0
        if self._player:
            try:
                self._player.pause = True
                self._player.seek(pos_sec, "absolute")
                self._player.speed = 1.0
                self._current_speed = 1.0
                self._initial_sync_done = False
            except Exception as e:
                print(f"[MPV] Seek error: {e}")
        if self._current_uri:
            def work():
                global seek_base_pos
                audio = self._resolved_audio_url
                if not audio:
                    print("[SEEK] WARNING: No resolved audio URL available, seeking mpv only")
                    self._sync_ready = True
                    return
                set_audio_stream(audio, seek_sec=pos_sec, headers=getattr(self, '_resolved_audio_headers', None))
                start_sonos_stream()
                sonos_ok = wait_for_sonos_playing()
                if sonos_ok:
                    seek_base_pos = pos_sec
                    self._sync_ready = True
                    self._initial_sync_done = True
                    if self._player:
                        try:
                            self._player.pause = False
                            self._player.seek(pos_sec, "absolute")
                            self._player.speed = 1.0
                            self._current_speed = 1.0
                        except Exception as e:
                            print(f"[SYNC] Seek resume error: {e}")
                    print(f"[SYNC] Seek: base={pos_sec:.1f}s, monitor will sync")
                else:
                    if self._player:
                        try:
                            self._player.pause = False
                        except Exception:
                            pass
                    self._status_signal.emit("Playing (video only)")
            threading.Thread(target=work, daemon=True).start()

    def browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Media File", "",
            "Media Files (*.mp4 *.mkv *.avi *.mov *.ts *.m4v *.webm *.flv *.mp3 *.flac *.wav *.ogg);;All Files (*)",
        )
        if path:
            self.uri_label.setText(path)

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

        def resolve_and_play():
            global seek_base_pos

            # Check if we have cached probe data from pre-play probing
            use_cache = (self._probe_data is not None
                         and self._last_probed_uri == uri
                         and self._probe_data.get("audio_tracks"))

            if use_cache and self._probe_data["type"] == "url":
                # Use cached probe data (avoids redundant yt-dlp call)
                data = self._probe_data
                _video_url = data.get("video_url", uri)
                title = data.get("title", uri)
                duration = data.get("duration", 0) or 0
                is_live = data.get("is_live", False) or False
                tracks = data.get("audio_tracks", [])
                sel = self._selected_audio_index
                if sel >= 0 and sel < len(tracks):
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
                _video_url, audio_url, title, duration, is_live, _video_headers, audio_headers = resolve_media_url(uri)

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
                except Exception:
                    pass
                self._player.play(uri)
                print(f"[MPV] Playing: {title}")
                self._schedule_color_log.emit()
            except Exception as e:
                print(f"[MPV] Play error: {e}")
                self._status_signal.emit(f"Error: {e}")
                return

            set_audio_stream(audio_url, seek_sec=0, headers=self._resolved_audio_headers)
            start_sonos_stream()
            sonos_ok = wait_for_sonos_playing()

            if is_live:
                seek_base_pos = 0.0
                self._sync_ready = True
                self._initial_sync_done = True
                self._current_speed = 1.0
                self._status_signal.emit(f"Playing LIVE: {title}")
                if self._player:
                    try:
                        self._player.pause = False
                        self._player.speed = 1.0
                    except Exception:
                        pass
            elif sonos_ok:
                seek_base_pos = 0.0
                self._sync_ready = True
                print(f"[SYNC] Initial: base=0, monitor will sync")
                self._status_signal.emit(f"Playing: {title}")
            else:
                if self._player:
                    try:
                        self._player.pause = False
                    except Exception:
                        pass
                self._status_signal.emit(f"Playing (video only): {title}")

        threading.Thread(target=resolve_and_play, daemon=True).start()

    def do_stop(self):
        global audio_uri, audio_seek_offset, ffmpeg_process, seek_base_pos
        seek_base_pos = 0.0
        self._current_speed = 1.0
        self._sync_ready = False
        self._initial_sync_done = False
        self._last_soco_elapsed = 0.0
        self._last_soco_time = 0.0
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
            except Exception:
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
            except Exception:
                pass
        threading.Thread(target=set_speaker_vol, daemon=True).start()

    def _edge_hit(self, pos):
        rect = self.rect()
        m = self._RESIZE_MARGIN
        x, y = pos.x(), pos.y()
        edge = 0
        if x < m:
            edge |= Qt.LeftEdge
        elif x > rect.width() - m:
            edge |= Qt.RightEdge
        if y < m:
            edge |= Qt.TopEdge
        elif y > rect.height() - m:
            edge |= Qt.BottomEdge
        return edge

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            edge = self._edge_hit(event.pos())
            if edge:
                self._resize_edge = edge
                self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
                event.accept()
            elif event.pos().y() < 36:
                self._dragging = True
                self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
                event.accept()
            else:
                self._resize_edge = None

    def mouseMoveEvent(self, event):
        if getattr(self, '_dragging', False) and self._drag_pos:
            self.move(event.globalPos() - self._drag_pos)
            event.accept()
            return
        if self._resize_edge:
            diff = event.globalPos() - self._drag_pos
            geom = self.frameGeometry()
            new_pos = geom.topLeft()

            if self._resize_edge & Qt.LeftEdge:
                new_rect = QRect(diff.x(), geom.y(), geom.width() - diff.x() + geom.x(), geom.height())
                if new_rect.width() >= self.minimumWidth():
                    new_pos.setX(diff.x())
                    geom.setWidth(new_rect.width())
            if self._resize_edge & Qt.TopEdge:
                new_rect = QRect(geom.x(), diff.y(), geom.width(), geom.height() - diff.y() + geom.y())
                if new_rect.height() >= self.minimumHeight():
                    new_pos.setY(diff.y())
                    geom.setHeight(new_rect.height())
            if self._resize_edge & Qt.RightEdge:
                geom.setWidth(max(self.minimumWidth(), diff.x() - geom.x() + 1))
            if self._resize_edge & Qt.BottomEdge:
                geom.setHeight(max(self.minimumHeight(), diff.y() - geom.y() + 1))

            self.setGeometry(geom)
            event.accept()
        else:
            edge = self._edge_hit(event.pos())
            if edge & Qt.LeftEdge or edge & Qt.RightEdge:
                self.setCursor(Qt.SizeHorCursor)
            elif edge & Qt.TopEdge or edge & Qt.BottomEdge:
                self.setCursor(Qt.SizeVerCursor)
            else:
                self.setCursor(Qt.ArrowCursor)

    def mouseReleaseEvent(self, event):
        self._resize_edge = None
        self._drag_pos = None
        self._dragging = False
        self.setCursor(Qt.ArrowCursor)

    def closeEvent(self, event):
        if self._player:
            try:
                self._player.terminate()
            except Exception:
                pass
        stop_sonos_stream()
        event.accept()


if __name__ == "__main__":
    print("Stream: http://" + PC_IP + ":" + str(stream_port) + "/stream.mp3")
    print("Sonos speaker: " + str(sonos_ip))

    threading.Thread(target=start_stream_server, daemon=True).start()

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    qt_app = QApplication(sys.argv)
    qt_app.setStyleSheet(DARK_STYLE)
    main_window = MainWindow()
    main_window.show()
    main_window.adjustSize()

    sys.exit(qt_app.exec_())
