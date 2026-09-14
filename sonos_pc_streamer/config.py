"""Configuration, environment, and tool discovery for Sonos PC Streamer.

Imported by main.py BEFORE ``import mpv``: performs the PATH/DLL setup mpv
and the yt-dlp CLI need, loads .env, and discovers ffmpeg/ffprobe. This
module must never import mpv/soco/yt_dlp/PySide6.
"""

import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil
from dotenv import load_dotenv

from sonos_pc_streamer.logging_utils import timestamped_print as _print

# Repo root (config.py lives in sonos_pc_streamer/): every filesystem anchor
# below — mpv-bin, .venv, ffmpeg, settings.json — must hang off this dir.
ROOT = str(Path(__file__).resolve().parents[1])

MPV_DIR = os.path.join(ROOT, "mpv-bin")
os.environ["PATH"] = MPV_DIR + os.pathsep + os.environ["PATH"]

# Add venv Scripts to PATH for yt-dlp CLI access
_venv_scripts = os.path.join(ROOT, ".venv", "Scripts")
if os.path.isdir(_venv_scripts) and _venv_scripts not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _venv_scripts + os.pathsep + os.environ["PATH"]

if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
    os.add_dll_directory(MPV_DIR)

load_dotenv()
sonos_ip = os.getenv("SONOS_IP")
stream_port = int(os.getenv("STREAM_PORT", "8002"))


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


_PC_IP = None


def local_ip():
    """Cached local IP for the stream URI; resolved on first use instead of
    at import, so importing this module has no network side effects."""
    global _PC_IP
    if _PC_IP is None:
        try:
            _PC_IP = get_local_ip()
        except (Exception,):
            _PC_IP = "127.0.0.1"
    return _PC_IP


def boost_process_priority():
    try:
        p = psutil.Process(os.getpid())
        p.nice(psutil.HIGH_PRIORITY_CLASS)
    except (Exception,) as e:
        _print(f"[CONFIG] Boost process priority failed: {e}")


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
                    _print(f"[CLEANUP] Killing PID {pid} ({proc.name()}) on port {port}")
                    proc.terminate()
                    proc.wait(timeout=3)
                except (Exception,) as e:
                    _print(f"[CLEANUP] Kill PID {pid} failed: {e}")
                return True
    except (Exception,) as e:
        _print(f"[CLEANUP] netstat scan failed: {e}")
    return False


def _find_ff_tool(tool_name: str):
    ffmpeg_root = os.path.join(ROOT, "ffmpeg")
    if os.path.isdir(ffmpeg_root):
        for build_dir in glob.glob(os.path.join(ffmpeg_root, "ffmpeg-*-essentials_build")):
            candidate = os.path.join(build_dir, "bin", f"{tool_name}.exe")
            if os.path.isfile(candidate):
                return candidate
    return shutil.which(tool_name)


def find_ffmpeg():
    return _find_ff_tool("ffmpeg")


def find_ffprobe():
    return _find_ff_tool("ffprobe")


FFMPEG_PATH = find_ffmpeg()
_print("FFmpeg:", FFMPEG_PATH)
FFPROBE_PATH = find_ffprobe()


def require_ffmpeg():
    if not FFMPEG_PATH:
        _print("ERROR: ffmpeg not found.")
        sys.exit(1)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


_settings_file = os.path.join(ROOT, "settings.json")


def _load_settings_file():
    """Read settings.json once; a missing/corrupt file yields {}."""
    try:
        with open(_settings_file) as f:
            return json.load(f)
    except (Exception,):
        return {}


def load_settings():
    """Return a shallow copy of the cached settings (loaded from disk once)."""
    return dict(_SETTINGS)


def save_settings(data):
    """Atomically persist settings via a temp file in the same directory."""
    global _SETTINGS
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", delete=False,
                                         dir=os.path.dirname(_settings_file) or ".",
                                         suffix=".tmp") as tmp:
            tmp_path = tmp.name
            json.dump(data, tmp, indent=2)
            tmp.flush()
        os.replace(tmp_path, _settings_file)
    except (Exception,) as e:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except (Exception,) as rm_e:
                _print(f"[SETTINGS] Failed to remove temp file {tmp_path}: {rm_e}")
        _print(f"[SETTINGS] Failed to save: {e}")
    else:
        _SETTINGS = dict(data)


_SETTINGS = _load_settings_file()

SYNC_OFFSET_SECONDS = 0.0
try:
    SYNC_OFFSET_SECONDS = float(_SETTINGS.get("sync_offset_seconds", 0.0) or 0.0)
except (Exception,):
    SYNC_OFFSET_SECONDS = 0.0
