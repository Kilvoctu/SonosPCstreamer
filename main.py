import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import re
import time

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
from PyQt5.QtCore import Qt, QTimer, QPoint, QRect, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

load_dotenv()
sonos_ip = os.getenv("SONOS_IP")
stream_port = int(os.getenv("STREAM_PORT", 8002))

audio_uri = None
audio_seek_offset = 0
sonos_playing = False
ffmpeg_process = None
ffmpeg_lock = threading.Lock()
seek_base_pos = 0.0


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
    bundled = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "ffmpeg", "ffmpeg-8.1-essentials_build", "bin", "ffmpeg.exe",
    )
    if os.path.isfile(bundled):
        return bundled
    ffmpeg_path = shutil.which("ffmpeg")
    if ffmpeg_path:
        return ffmpeg_path
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
        global ffmpeg_process
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

        cmd = [FFMPEG_PATH, "-re"]
        if audio_seek_offset > 0:
            cmd += ["-ss", str(audio_seek_offset), "-i", audio_uri]
        else:
            cmd += ["-i", audio_uri]
        cmd += ["-vn", "-acodec", "libmp3lame", "-ab", "192k",
                "-ac", "2", "-ar", "44100", "-f", "mp3", "pipe:1"]
        print(f"[STREAM] Starting ffmpeg: {audio_uri} (seek={audio_seek_offset}s)")
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
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
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


def set_audio_stream(uri, seek_sec=0):
    global audio_uri, audio_seek_offset
    audio_uri = uri
    audio_seek_offset = seek_sec
    print(f"[AUDIO] Stream offset set to {seek_sec}s")


_YT_URL_RE = re.compile(
    r'(?:https?://)?'
    r'(?:www\.)?'
    r'(?:youtube\.com/(?:watch\?.*?v=|live/|shorts/)|youtu\.be/)'
    r'[\w-]+'
)


def is_youtube_url(url):
    return bool(_YT_URL_RE.match(url.strip()))


def resolve_youtube_url(url):
    """Resolve a YouTube URL to direct video + audio CDN URLs.
    
    Returns (video_url, audio_url, title, duration, is_live, video_headers) or
    (None, None, None, 0, False, {}) on failure.
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
                print("[YT] Failed to extract info")
                return None, None, None, 0, False, {}
            video_url = info.get("url")
            audio_url = info.get("url")
            title = info.get("title", "Unknown")
            duration = info.get("duration", 0) or 0
            is_live = info.get("is_live", False) or False
            video_headers = {}
            if "formats" in info:
                best_audio = None
                for fmt in info["formats"]:
                    if fmt.get("acodec", "none") != "none" and fmt.get("vcodec", "none") == "none":
                        best_audio = fmt
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
            print(f"[YT] Resolved: {title} (live={is_live}, dur={duration}s)")
            return video_url, audio_url, title, duration, is_live, video_headers
    except Exception as e:
        print(f"[YT] Resolution failed: {e}")
        return None, None, None, 0, False, {}


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
        self._hdr_enabled = load_settings().get("hdr_enabled", False)
        self._sync_ready = False
        self._initial_sync_done = False
        self._last_soco_elapsed = 0.0
        self._last_soco_time = 0.0

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
        transport_row.addStretch()
        content_layout.addLayout(transport_row)

        seek_row = QHBoxLayout()
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setStyleSheet("color: #888; font-size: 12px; min-width: 100px;")
        seek_row.addWidget(self.time_label)
        self.sonos_label = QLabel("")
        self.sonos_label.setStyleSheet("color: #888; font-size: 11px; min-width: 120px;")
        seek_row.addWidget(self.sonos_label)
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

        self._sonos_timer = QTimer(self)
        self._sonos_timer.timeout.connect(self._poll_sonos_position)
        self._sonos_timer.start(250)

        try:
            self.vol_slider.setValue(speaker.volume)
            self.vol_label.setText(f"Volume: {speaker.volume}")
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
        if not self._sync_ready or self._seeking or getattr(self, '_sonos_busy', False):
            return
        self._sonos_busy = True
        threading.Thread(target=self._fetch_sonos_pos, daemon=True).start()

    def _fetch_sonos_pos(self):
        global seek_base_pos
        try:
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

            if not self._initial_sync_done or abs(drift) > 0.5:
                self._player.pause = True
                self._player.seek(audio_pos, "absolute")
                self._player.pause = False
                print(f"[SYNC] {'Init' if not self._initial_sync_done else 'Drift'} sync: drift={drift:.1f}s → {audio_pos:.1f}s")
                self._initial_sync_done = True
        except Exception:
            pass
        finally:
            self._sonos_busy = False

    def _on_sonos_pos(self, pos_str, audio_pos):
        if audio_pos > 0:
            self.sonos_label.setText(f"Sonos: {fmt_time(int(audio_pos*1000))}")
        else:
            self.sonos_label.setText(f"Sonos: {pos_str}")

    def _ensure_player(self):
        if self._player is not None:
            return True
        try:
            self._player = _mpv.MPV(
                geometry="1280x720",
                autofit="1280x720",
                title="Sonos PC Streamer - Video",
                pause=True,
                vo="gpu-next",
                gpu_api="d3d11",
                ytdl=True,
                ytdl_format="bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestaudio/best",
            )
            self._player.ao = "null"
            if self._hdr_enabled:
                self._player.target_prim = "bt.2020"
                self._player.target_trc = "pq"
                print("[MPV] HDR target: bt.2020/pq")
            else:
                print("[MPV] HDR target: default (SDR)")
            self._player.target_colorspace_hint = "yes"
            self._player.target_colorspace_hint_mode = "target"
            print("[MPV] Player created via libmpv")
            return True
        except Exception as e:
            print(f"[MPV] Failed to create player: {e}")
            self.status_label.setText(f"mpv error: {e}")
            return False

    def _toggle_hdr(self, checked):
        self._hdr_enabled = checked
        self.hdr_btn.setText("HDR: ON" if checked else "HDR: OFF")
        save_settings({"hdr_enabled": checked})
        self._apply_hdr_settings()
        print(f"[HDR] Toggle: {'ON' if checked else 'OFF'}")

    def _apply_hdr_settings(self):
        if not self._player:
            return
        try:
            if self._hdr_enabled:
                self._player.target_prim = "bt.2020"
                self._player.target_trc = "pq"
            else:
                self._player.target_prim = "auto"
                self._player.target_trc = "auto"
            self._schedule_color_log.emit()
        except Exception as e:
            print(f"[HDR] Apply error: {e}")

    def _log_color_info(self):
        """Log actual color state after content is loaded."""
        if not self._player:
            return
        parts = []
        for name in ("current_vo", "target_colorspace_hint",
                      "target_colorspace_hint_mode", "target_prim",
                      "target_trc", "gamut_mapping_mode",
                      "colormatrix", "colorlevels",
                      "hwdec_current"):
            try:
                val = getattr(self._player, name)
                parts.append(f"{name}={val}")
            except Exception:
                parts.append(f"{name}=n/a")
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
            except Exception as e:
                print(f"[MPV] Seek error: {e}")
        if self._current_uri:
            def work():
                global seek_base_pos
                uri = getattr(self, '_resolved_audio_url', None) or self._current_uri
                set_audio_stream(uri, seek_sec=pos_sec)
                start_sonos_stream()
                sonos_ok = wait_for_sonos_playing()
                if sonos_ok:
                    seek_base_pos = pos_sec
                    self._sync_ready = True
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

        # Check if this is a YouTube URL
        if is_youtube_url(uri):
            self.status_label.setText("Resolving YouTube URL...")
            print(f"[YT] Detected YouTube URL: {uri}")

            def resolve_and_play():
                global seek_base_pos
                _video_url, audio_url, title, duration, is_live, _video_headers = resolve_youtube_url(uri)
                if audio_url is None:
                    audio_url = uri
                    title = title or uri
                    is_live = False

                print(f"[YT] Resolved: {title} (live={is_live}, dur={duration}s)")
                print(f"[YT] audio_url: {audio_url[:120] if audio_url else 'None'}...")

                self._resolved_audio_url = audio_url
                self._is_live = is_live

                if is_live:
                    self._is_live = True
                    self.seek_slider.setEnabled(False)
                    self.time_label.setText("LIVE / --:--")
                    print(f"[YT] Live stream detected: {title}")
                else:
                    self.seek_slider.setEnabled(True)

                try:
                    self._player.pause = True
                    self._player.play(uri)
                    print(f"[MPV] Playing YouTube via ytdl hook: {title}")
                    self._schedule_color_log.emit()
                except Exception as e:
                    print(f"[MPV] Play error: {e}")
                    self._status_signal.emit(f"Error: {e}")
                    return

                set_audio_stream(audio_url, seek_sec=0)
                start_sonos_stream()
                sonos_ok = wait_for_sonos_playing()

                if is_live:
                    self._status_signal.emit(f"Playing LIVE: {title}")
                    if self._player:
                        try:
                            self._player.pause = False
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
            return

        # Non-YouTube: play directly
        try:
            self._player.pause = True
            self._player.play(uri)
            print(f"[MPV] Playing: {uri}")
            self._schedule_color_log.emit()
        except Exception as e:
            print(f"[MPV] Play error: {e}")
            self.status_label.setText(f"Error: {e}")
            return

        def work():
            global seek_base_pos
            set_audio_stream(uri, seek_sec=0)
            start_sonos_stream()
            sonos_ok = wait_for_sonos_playing()
            if sonos_ok:
                seek_base_pos = 0.0
                self._sync_ready = True
                print(f"[SYNC] Initial: base=0, monitor will sync")
                self._status_signal.emit("Playing")
            else:
                if self._player:
                    try:
                        self._player.pause = False
                    except Exception:
                        pass
                self._status_signal.emit("Playing (video only)")

        threading.Thread(target=work, daemon=True).start()

    def do_stop(self):
        global audio_uri, audio_seek_offset, ffmpeg_process, seek_base_pos
        seek_base_pos = 0.0
        self._sync_ready = False
        self._initial_sync_done = False
        self._last_soco_elapsed = 0.0
        self._last_soco_time = 0.0
        audio_uri = None
        audio_seek_offset = 0
        self._current_uri = None
        self._is_live = False
        self._resolved_audio_url = None
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
