"""Stream/HTTP/ffmpeg subsystem: session records, the /stream.mp3 HTTP
handler, and the ffmpeg lifecycle. No Qt/mpv/soco/yt_dlp/media imports.

Cross-module mutables written from the GUI (main.py) and read here are
managed through accessors: selected_audio_track and the seek worker's
output-seek-only fallback flag.
"""

import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sonos_pc_streamer.config import CREATE_NO_WINDOW, FFMPEG_PATH, stream_port
from sonos_pc_streamer.logging_utils import timestamped_print as _print

_stream_gen = 0                    # bumped on every set_audio_stream() call
stream_lock = threading.Lock()
http_server: ThreadingHTTPServer | None = None   # the ThreadingHTTPServer serving /stream.mp3 (guarded by http_server_lock)
http_server_lock = threading.Lock()
ffmpeg_process: subprocess.Popen | None = None
ffmpeg_lock = threading.Lock()
_selected_audio_track = -1   # -1 = default (stream 0), 0+ = specific track index
_output_seek_only = False    # set by seek worker fallback: source can't range-seek


def set_selected_audio_track(idx):
    global _selected_audio_track
    _selected_audio_track = idx


def get_selected_audio_track():
    return _selected_audio_track


def set_output_seek_only(flag):
    global _output_seek_only
    _output_seek_only = flag


def get_output_seek_only():
    return _output_seek_only


@dataclass(frozen=True)
class StreamSession:
    """Immutable snapshot of one active stream request/session.

    Request fields are fixed at creation and never rewritten. Only the
    ``process`` slot is bound later by the owning handler thread, while
    holding ``stream_lock`` — so a superseded session can never adopt a
    process, and a replacement can safely terminate whatever is attached.
    ``stderr_tail`` is written only by that session's own stderr drain
    thread, so concurrent handlers never pollute each other's diagnostics.
    """

    generation: int
    uri: str
    seek_offset: float = 0.0
    headers: dict = field(default_factory=dict)
    selected_audio_track: int = -1
    output_seek_only: bool = False
    first_byte_event: threading.Event = field(default_factory=threading.Event)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    stderr_tail: list = field(default_factory=list)
    process: subprocess.Popen | None = None

    def attach_process(self, proc):
        # frozen record: the process slot is the one late-bound field
        object.__setattr__(self, "process", proc)

    def detach_process(self, proc):
        # only the owning handler may clear its own association
        if self.process is proc:
            object.__setattr__(self, "process", None)


active_session: StreamSession | None = None   # the one StreamSession handlers may serve (guarded by stream_lock)


def kill_ffmpeg_process(proc):
    """Kill ffmpeg process if it is still running (idempotent, never blocks).

    Safe on already-exited processes: poll() reporting an exit code skips the
    kill entirely, and a failing poll() (or kill()) is swallowed so callers
    can invoke this repeatedly without ever raising.
    """
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return  # already exited — nothing to kill
    except (Exception,) as e:
        _print(f"[STREAM] ffmpeg state check failed: {e}")  # state unknown — attempt the kill anyway
    try:
        proc.kill()
    except (Exception,) as exc:
        _print(f"[STREAM] ffmpeg kill failed: {exc}")


def stop_active_stream_session():
    """Cancel + detach the active stream session and kill its ffmpeg process.

    The kill happens outside stream_lock and never waits on the process;
    the owning handler notices the cancellation/EOF on its own. Returns the
    canceled session or None.
    """
    global active_session, ffmpeg_process
    with stream_lock:
        session = active_session
        active_session = None
        proc = session.process if session is not None else None
        if session is not None:
            session.cancel_event.set()
    if proc is not None:
        kill_ffmpeg_process(proc)
        with ffmpeg_lock:
            if ffmpeg_process is proc:
                ffmpeg_process = None
    return session


def active_stream_stderr_tail():
    """Joined ffmpeg stderr tail of the active session, for failure diagnostics."""
    with stream_lock:
        session = active_session
        lines = list(session.stderr_tail) if session is not None else []
    return " ".join(lines)


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

        # Capture exactly one snapshot of the active session; everything below
        # (uri, headers, seek, track, gen, first-byte event, fallback mode) is
        # taken from that snapshot so a mid-request replacement can never mix
        # generations or keep writing stale audio after it.
        with stream_lock:
            session = active_session
        if session is None or not session.uri or session.cancel_event.is_set():
            _print("[STREAM] No active stream session")
            return
        t0 = time.time()

        uri = session.uri
        seek_offset = session.seek_offset
        is_http = uri.startswith("http")
        fallback_output_seek = session.output_seek_only and is_http

        merged_headers = dict(session.headers) if session.headers else {}
        if is_http:
            if not any(k.lower() == "user-agent" for k in merged_headers):
                merged_headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
            if ("youtube.com" in uri or "googlevideo.com" in uri) \
                    and not any(k.lower() in ("origin", "referer") for k in merged_headers):
                merged_headers["Origin"] = "https://www.youtube.com"
                merged_headers["Referer"] = "https://www.youtube.com/"
        headers_arg = "".join(f"{k}: {v}\r\n" for k, v in merged_headers.items()) if merged_headers else None
        ua_present = any(k.lower() == "user-agent" for k in merged_headers)
        _print(f"[STREAM] HTTP headers: {', '.join(sorted(merged_headers.keys())) or 'none'} (UA: {'yes' if ua_present else 'NO'})")
        cmd = [FFMPEG_PATH, "-re"]
        if headers_arg:
            cmd += ["-headers", headers_arg]
        if is_http:
            cmd += ["-reconnect", "1", "-reconnect_delay_max", "5"]
        if seek_offset > 0 and not fallback_output_seek:
            cmd += ["-ss", str(seek_offset)]      # input seek (fast) for local and http
        cmd += ["-i", uri]
        if seek_offset > 0 and fallback_output_seek:
            cmd += ["-ss", str(seek_offset)]      # fallback: realtime output seek after -i
        if session.selected_audio_track >= 0 and not is_http:
            cmd += ["-map", f"0:a:{session.selected_audio_track}"]
            _print(f"[STREAM] Mapping audio stream: 0:a:{session.selected_audio_track}")
        cmd += ["-vn", "-acodec", "libmp3lame", "-ab", "192k",
                "-ac", "2", "-ar", "44100", "-f", "mp3", "pipe:1"]
        if session.cancel_event.is_set():
            # Superseded while the request was being prepared — do not spawn.
            _print(f"[STREAM] Session gen {session.generation} cancelled before ffmpeg start")
            return
        _print(f"[STREAM] Starting ffmpeg: {uri} (seek_offset={seek_offset}s, gen={session.generation}, fallback={fallback_output_seek})")
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
            )
        except (Exception,) as e:
            _print("[STREAM] Failed to start ffmpeg:", e)
            return

        stdout = proc.stdout
        stderr_pipe = proc.stderr
        if stdout is None:
            # PIPE was requested for both; unreachable in practice, kept so
            # the types below are provably non-optional.
            kill_ffmpeg_process(proc)
            return
        if stderr_pipe is None:
            kill_ffmpeg_process(proc)
            return

        def _drain_ffmpeg_stderr(stderr_reader, session_snapshot):
            try:
                for raw in iter(stderr_reader.readline, b""):
                    line = raw.decode("utf-8", errors="replace").rstrip()
                    if line:
                        session_snapshot.stderr_tail.append(line)
                        if len(session_snapshot.stderr_tail) > 40:
                            session_snapshot.stderr_tail.pop(0)
            except (Exception,) as drain_exc:
                _print(f"[STREAM] ffmpeg stderr drain failed: {drain_exc}")

        threading.Thread(target=_drain_ffmpeg_stderr, args=(stderr_pipe, session), daemon=True).start()

        # Own this process only while the session is still the active one; a
        # handler that lost the race against a replacement must never adopt
        # (or kill) a newer session's process.
        with stream_lock:
            if active_session is session and not session.cancel_event.is_set():
                if session.process is None:
                    session.attach_process(proc)
                    with ffmpeg_lock:
                        ffmpeg_process = proc   # compatibility/debug mirror
                else:
                    # Another concurrent handler already owns this session's
                    # process slot — this handler lost the ownership race.
                    _print(f"[STREAM] Session gen {session.generation} already owned by another handler")
                    kill_ffmpeg_process(proc)   # only our own local process
                    return
            else:
                _print(f"[STREAM] Session gen {session.generation} superseded during ffmpeg startup")
                kill_ffmpeg_process(proc)
                return

        first_chunk = True
        try:
            while True:
                if session.cancel_event.is_set() or active_session is not session:
                    _print(f"[STREAM] Session gen {session.generation} superseded — stopping stream")
                    break
                chunk = stdout.read(1024)
                if not chunk:
                    break
                if first_chunk:
                    first_chunk = False
                    _print(f"[STREAM] First audio byte after {time.time() - t0:.2f}s (gen {session.generation})")
                    session.first_byte_event.set()
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            _print(f"[STREAM] Connection lost: {type(exc).__name__}: {exc}")
        finally:
            kill_ffmpeg_process(proc)   # idempotent — may already be dead
            session.detach_process(proc)
            with ffmpeg_lock:
                if ffmpeg_process is proc:
                    ffmpeg_process = None
            _print("[STREAM] ffmpeg process ended")
            if first_chunk:
                tail = " | ".join(session.stderr_tail[-6:]) if session.stderr_tail else "no stderr output"
                _print(f"[STREAM] No audio was produced — ffmpeg stderr tail: {tail}")

    def log_message(self, *args, **kwargs):
        pass


def start_stream_server():
    """Serve the stream HTTP server until shutdown_stream_server() stops it.

    The server is published in ``http_server`` right after construction so
    closeEvent can shut it down deterministically. The finally block runs in
    the serve thread: it un-publishes the reference and closes the socket
    (server_close is idempotent — a second close after an external shutdown
    is harmless).
    """
    global http_server
    # noinspection PyTypeChecker
    server = ThreadingHTTPServer(("0.0.0.0", stream_port), StreamHandler)
    with http_server_lock:
        http_server = server
    _print(f"[STREAM] Listening on port {stream_port}")
    try:
        server.serve_forever()
    finally:
        with http_server_lock:
            if http_server is server:
                http_server = None
        try:
            server.server_close()
        except (Exception,) as e:
            _print(f"[STREAM] HTTP server close failed: {e}")
        _print("[STREAM] HTTP server stopped")


def shutdown_stream_server():
    """Stop the streaming HTTP server from a non-serve thread (idempotent).

    Safe when startup has not completed or the server already stopped (no-op).
    The reference is claimed under the lock, then shutdown()/server_close()
    run without holding it. Must NOT be called from the serve_forever()
    thread itself — shutdown() waits for serve_forever() to return and would
    deadlock there; the GUI thread is the expected caller.
    """
    global http_server
    with http_server_lock:
        server = http_server
        if server is None:
            return
        http_server = None
    try:
        server.shutdown()
    except (Exception,) as e:
        _print(f"[STREAM] HTTP server shutdown failed: {e}")
    try:
        server.server_close()
    except (Exception,) as exc:
        _print(f"[STREAM] HTTP server close failed: {exc}")


def set_audio_stream(uri, seek_sec=0, headers=None):
    global _stream_gen, active_session, ffmpeg_process
    headers = headers or {}
    with stream_lock:
        old_session = active_session
        old_proc = old_session.process if old_session is not None else None
        _stream_gen += 1
        new_session = StreamSession(
            generation=_stream_gen,
            uri=uri,
            seek_offset=seek_sec,
            headers=dict(headers),
            selected_audio_track=_selected_audio_track,
            output_seek_only=_output_seek_only,
        )
        active_session = new_session
        if old_session is not None:
            old_session.cancel_event.set()
    # Terminate the superseded session's process outside the lock; never
    # block here — the old handler cleans up when it notices the cancellation.
    # Drop the compat mirror if it still refers to the old process so
    # wait_for_sonos_audio() can't see the dead/old process after replacement.
    if old_session is not None:
        kill_ffmpeg_process(old_proc)
        if old_proc is not None:
            with ffmpeg_lock:
                if ffmpeg_process is old_proc:
                    ffmpeg_process = None
    _print(f"[AUDIO] Stream offset set to {seek_sec}s (gen {_stream_gen})")
