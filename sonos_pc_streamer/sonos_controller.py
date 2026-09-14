"""Sonos device control for Sonos PC Streamer.

Extracted from main.py: the shared SoCo speaker handle, the device I/O
lock, stream start/stop, and the PLAYING/audio-bytes wait loop. UI-free
on purpose (no Qt, no mpv, no yt_dlp) -- dependency direction is
sonos_controller -> config/stream_server/logging_utils only.
"""

import threading
import time

import soco

from sonos_pc_streamer import stream_server
from sonos_pc_streamer.config import local_ip, sonos_ip, stream_port
from sonos_pc_streamer.logging_utils import timestamped_print as _print

sonos_lock = threading.Lock()     # serializes all SoCo device I/O; SoCo is not thread-safe


speaker = soco.SoCo(sonos_ip)


def wait_for_sonos_audio(play_timeout=60.0, audio_timeout=12.0, fail_fast_timeout=10.0):
    """Wait for Sonos PLAYING plus actual audio bytes flowing from the stream.

    Returns "ok", "timeout" (Sonos never reached PLAYING), or "no_audio".
    Fail-fast: if ffmpeg died without ever producing audio, return "no_audio"
    after fail_fast_timeout instead of waiting out the full Sonos timeout.
    """
    with stream_server.stream_lock:
        session = stream_server.active_session
    started = time.time()
    ever_playing = False
    deadline = started + play_timeout
    while time.time() < deadline:
        try:
            with sonos_lock:
                info = speaker.get_current_transport_info()
            if info.get("current_transport_state", "") == "PLAYING":
                ever_playing = True
                break
        except (Exception,) as e:
            _print(f"[SONOS] Transport poll failed: {e}")
        if (not ever_playing and (session is None or session.process is None)
                and (time.time() - started) >= fail_fast_timeout):
            _print("[SYNC] ffmpeg died without producing audio — failing fast")
            return "no_audio"
        time.sleep(0.5)
    if not ever_playing:
        _print("[SYNC] Sonos did not start playing in time")
        return "timeout"
    ev = session.first_byte_event if session is not None else None
    if ev is None:
        return "ok"
    if ev.wait(timeout=audio_timeout):
        return "ok"
    _print("[SYNC] Sonos PLAYING but no audio bytes within timeout")
    return "no_audio"


def start_sonos_stream():
    stream_uri = "http://" + local_ip() + ":" + str(stream_port) + "/stream.mp3"
    _print("[SONOS] Connecting to stream:", stream_uri)
    with sonos_lock:
        speaker.stop()
        speaker.play_uri(stream_uri)
    _print("[SONOS] play_uri sent")


def stop_sonos_stream():
    try:
        with sonos_lock:
            speaker.stop()
    except (Exception,) as e:
        _print(f"[SONOS] Stop failed: {e}")
