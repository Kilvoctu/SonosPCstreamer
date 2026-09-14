"""Focused, no-network tests for stream-session ownership in main.py.

Imports main with soco/mpv/yt_dlp stubbed out and socket connects disabled,
so nothing touches the Sonos device, YouTube, or a live HTTP server. Covers:

1. A second concurrent handler for an already-owned active StreamSession is
   rejected: only its own freshly spawned ffmpeg is killed, the attached
   owner process and the ffmpeg_process mirror are untouched, and it returns
   before entering the output loop.
2. The pre-existing supersession behavior is preserved: a handler whose
   session is no longer active at the process-association block kills only
   its own local ffmpeg and never adopts a newer session's process.
3. set_audio_stream() replacement kills the old process and immediately
   clears the ffmpeg_process compatibility mirror when it still refers to the
   old process, while never clearing a newer/unrelated process.

4. Shutdown determinism (no network, no real external port):
   shutdown_stream_server() is a no-op when no server exists, stops/closes
   and un-registers a live server exactly once (idempotent), the full
   start_stream_server() lifecycle shuts down without deadlock on an
   OS-assigned ephemeral port, kill_ffmpeg_process() is safe/idempotent on
   exited, raising-poll, and raising-kill processes, and sync_monitor_loop()
   exits promptly once MainWindow.shutdown_event is set.
"""

import io
import socket
import threading
import time
import unittest

from _import_main import import_main_no_network as _import_main_no_network

from sonos_pc_streamer import stream_server


class FakeStdout:
    def __init__(self, chunks=b"", gate=None):
        self._chunks = chunks
        self._gate = gate

    def read(self, _size=-1):
        if self._gate is not None:
            self._gate.wait(timeout=10)
        data, self._chunks = self._chunks, b""
        return data


class FakeStderr:
    @staticmethod
    def readline():
        return b""


class FakePopen:
    """Minimal stand-in for subprocess.Popen as used by handle_stream()."""

    def __init__(self, cmd, chunks=b"", gate=None, **_kwargs):
        self.cmd = cmd
        self.stdout = FakeStdout(chunks, gate)
        self.stderr = FakeStderr()
        self.pid = id(self)
        self.killed = False

    def poll(self):
        return 0 if self.killed else None

    def kill(self):
        self.killed = True


class FakeHandler:
    """Just enough of a BaseHTTPRequestHandler for handle_stream()."""

    def __init__(self):
        self.path = "/stream.mp3"
        self.wfile = io.BytesIO()

    def send_response(self, *args):
        pass

    def send_header(self, *args):
        pass

    def end_headers(self):
        pass


class StreamSessionOwnershipTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_no_network()

    def setUp(self):
        stream_server.stop_active_stream_session()
        with stream_server.ffmpeg_lock:
            stream_server.ffmpeg_process = None

    def test_duplicate_handler_ownership_rejected(self):
        stream_server.set_audio_stream("http://source.example/track", seek_sec=0)
        session = stream_server.active_session
        self.assertIsNotNone(session)
        assert session is not None  # set_audio_stream() always publishes a session

        gate = threading.Event()
        configs = [
            {"chunks": b"A" * 2048, "gate": gate},   # owner: blocks in its output loop
            {"chunks": b"B" * 1024},                 # duplicate: data ready to stream
        ]
        created = []
        original_popen = stream_server.subprocess.Popen

        def fake_popen(cmd, **_kwargs):
            proc = FakePopen(cmd, **configs[len(created)])
            created.append(proc)
            return proc

        owner = FakeHandler()
        t = threading.Thread(
            target=stream_server.StreamHandler.handle_stream, args=(owner,), daemon=True)
        stream_server.subprocess.Popen = fake_popen
        try:
            t.start()
            deadline = time.time() + 5
            while time.time() < deadline and session.process is None:
                time.sleep(0.01)
            self.assertEqual(len(created), 1)
            owner_proc = created[0]
            self.assertIs(session.process, owner_proc)

            # Second concurrent handler for the same active session.
            duplicate = FakeHandler()
            stream_server.StreamHandler.handle_stream(duplicate)  # type: ignore

            self.assertEqual(len(created), 2)
            dup_proc = created[1]
            self.assertTrue(dup_proc.killed)                  # only the duplicate's proc dies
            self.assertFalse(owner_proc.killed)               # owner's attachment untouched
            self.assertIs(session.process, owner_proc)
            with stream_server.ffmpeg_lock:
                self.assertIs(stream_server.ffmpeg_process, owner_proc)   # mirror untouched
            self.assertEqual(duplicate.wfile.getvalue(), b"")    # never entered output loop
            self.assertFalse(session.first_byte_event.is_set())
        finally:
            gate.set()
            t.join(timeout=10)
            stream_server.subprocess.Popen = original_popen

        self.assertFalse(t.is_alive())
        self.assertTrue(session.first_byte_event.is_set())   # owner streamed normally
        self.assertTrue(owner_proc.killed)                   # owner's own cleanup killed it
        self.assertIsNone(session.process)
        with stream_server.ffmpeg_lock:
            self.assertIsNone(stream_server.ffmpeg_process)           # owner cleared the mirror

    def test_superseded_session_still_killed_during_startup(self):
        stream_server.set_audio_stream("http://source.example/orig", seek_sec=0)
        old_session = stream_server.active_session
        assert old_session is not None  # set_audio_stream() always publishes a session

        created = []

        def fake_popen(cmd, **_kwargs):
            proc = FakePopen(cmd, chunks=b"X" * 1024)
            created.append(proc)
            # Simulate a replacement landing while ffmpeg was starting up.
            stream_server.set_audio_stream("http://source.example/replacement", seek_sec=0)
            return proc

        original_popen = stream_server.subprocess.Popen
        stream_server.subprocess.Popen = fake_popen
        try:
            handler = FakeHandler()
            stream_server.StreamHandler.handle_stream(handler)  # type: ignore
        finally:
            stream_server.subprocess.Popen = original_popen

        self.assertEqual(len(created), 1)
        self.assertTrue(created[0].killed)                   # local proc killed, not adopted
        self.assertIsNone(old_session.process)               # superseded session never adopted it
        self.assertTrue(old_session.cancel_event.is_set())
        self.assertIsNot(stream_server.active_session, old_session)
        self.assertEqual(handler.wfile.getvalue(), b"")
        with stream_server.ffmpeg_lock:
            self.assertIsNone(stream_server.ffmpeg_process)           # mirror never pointed at it

    def test_set_audio_stream_clears_old_ffmpeg_mirror(self):
        stream_server.set_audio_stream("http://source.example/old", seek_sec=0)
        old_session = stream_server.active_session
        assert old_session is not None  # set_audio_stream() always publishes a session
        old_proc = FakePopen(b"O" * 1024)
        with stream_server.stream_lock:
            old_session.attach_process(old_proc)
        with stream_server.ffmpeg_lock:
            stream_server.ffmpeg_process = old_proc

        stream_server.set_audio_stream("http://source.example/new", seek_sec=5)

        self.assertTrue(old_proc.killed)                     # old process terminated
        self.assertTrue(old_session.cancel_event.is_set())
        self.assertIsNot(stream_server.active_session, old_session)
        with stream_server.ffmpeg_lock:
            self.assertIsNone(stream_server.ffmpeg_process)           # old process gone from the mirror

    def test_set_audio_stream_does_not_clear_newer_process(self):
        stream_server.set_audio_stream("http://source.example/old", seek_sec=0)
        old_session = stream_server.active_session
        assert old_session is not None  # set_audio_stream() always publishes a session
        old_proc = FakePopen(b"O" * 1024)
        newer = FakePopen(b"N" * 1024)
        with stream_server.stream_lock:
            old_session.attach_process(old_proc)
        with stream_server.ffmpeg_lock:
            stream_server.ffmpeg_process = newer                      # mirror already moved on

        stream_server.set_audio_stream("http://source.example/new", seek_sec=0)

        self.assertTrue(old_proc.killed)
        with stream_server.ffmpeg_lock:
            self.assertIs(stream_server.ffmpeg_process, newer)        # newer process preserved


class _CountingProc:
    """Minimal ffmpeg-process stand-in for kill_ffmpeg_process() probes."""

    def __init__(self, exited=False, poll_raises=False, kill_raises=False):
        self._rc = 0 if exited else None
        self._poll_raises = poll_raises
        self._kill_raises = kill_raises
        self.kill_calls = 0

    def poll(self):
        if self._poll_raises:
            raise OSError("poll failed")
        return self._rc

    def kill(self):
        self.kill_calls += 1
        if self._kill_raises:
            raise OSError("kill failed")
        self._rc = 1  # simulate termination so repeat kills become no-ops


class _FakeStreamServer:
    """Stand-in for ThreadingHTTPServer with observable shutdown/close."""

    def __init__(self):
        self.shutdown_calls = 0
        self.close_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1

    def server_close(self):
        self.close_calls += 1


class _StubSyncWindow:
    """Attribute surface consumed by sync_monitor_loop() before any sync work."""

    SONOS_POLL_INTERVAL = 30.0  # huge: only an interruptible wait can exit

    def __init__(self):
        self.shutdown_event = threading.Event()
        self.sync_ready = False
        self.seeking = False
        self.seek_in_progress = False


class StreamServerShutdownTest(unittest.TestCase):
    """No-network probes for deterministic shutdown paths in main.py."""

    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_no_network()

    def setUp(self):
        with stream_server.http_server_lock:
            stream_server.http_server = None

    def test_shutdown_stream_server_safe_without_server(self):
        # No server ever started: the helper must be a fast, silent no-op.
        self.assertIsNone(stream_server.http_server)
        start = time.perf_counter()
        stream_server.shutdown_stream_server()
        stream_server.shutdown_stream_server()
        self.assertLess(time.perf_counter() - start, 1.0)
        self.assertIsNone(stream_server.http_server)

    def test_shutdown_stream_server_stops_closes_and_clears_once(self):
        fake = _FakeStreamServer()
        with stream_server.http_server_lock:
            stream_server.http_server = fake

        stream_server.shutdown_stream_server()

        self.assertEqual(fake.shutdown_calls, 1)
        self.assertEqual(fake.close_calls, 1)
        self.assertIsNone(stream_server.http_server)

        stream_server.shutdown_stream_server()  # idempotent: reference already cleared

        self.assertEqual(fake.shutdown_calls, 1)
        self.assertEqual(fake.close_calls, 1)

    def test_start_stream_server_full_lifecycle_shutdown(self):
        original_port = stream_server.stream_port
        original_fqdn = socket.getfqdn
        stream_server.stream_port = 0  # OS-assigned ephemeral port — no external bind
        # server_bind() does a blocking reverse-DNS lookup (getfqdn); stub it
        # so the probe is deterministic on DNS-less machines.
        socket.getfqdn = lambda name="": "localhost"
        t = threading.Thread(target=stream_server.start_stream_server, daemon=True)
        t.start()
        try:
            deadline = time.time() + 5
            while time.time() < deadline and stream_server.http_server is None:
                time.sleep(0.01)
            self.assertIsNotNone(stream_server.http_server)

            stream_server.shutdown_stream_server()  # called from this (non-serve) thread
            t.join(timeout=5.0)
        finally:
            stream_server.stream_port = original_port
            socket.getfqdn = original_fqdn

        self.assertFalse(t.is_alive())       # serve_forever exited, no deadlock
        self.assertIsNone(stream_server.http_server)
        stream_server.shutdown_stream_server()       # idempotent after full lifecycle

    def test_kill_ffmpeg_process_robust_and_idempotent(self):
        kill = self.main.kill_ffmpeg_process

        kill(None)  # always a no-op

        dead = _CountingProc(exited=True)
        kill(dead)
        self.assertEqual(dead.kill_calls, 0)  # already exited — skip the kill

        alive = _CountingProc()
        kill(alive)
        self.assertEqual(alive.kill_calls, 1)
        kill(alive)  # process now dead (simulated) — second call is a no-op
        self.assertEqual(alive.kill_calls, 1)

        broken_poll = _CountingProc(poll_raises=True)
        kill(broken_poll)  # poll raising must still reach the kill
        self.assertEqual(broken_poll.kill_calls, 1)

        broken_kill = _CountingProc(kill_raises=True)
        kill(broken_kill)  # kill raising must be swallowed
        self.assertEqual(broken_kill.kill_calls, 1)

    def test_sync_monitor_loop_exits_when_shutdown_event_preset(self):
        main = self.main
        window = _StubSyncWindow()
        window.shutdown_event.set()

        t = threading.Thread(
            target=main.MainWindow.sync_monitor_loop, args=(window,), daemon=True)
        t.start()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())  # exits immediately, no 30s sleep

    def test_sync_monitor_loop_wakes_early_on_shutdown(self):
        main = self.main
        window = _StubSyncWindow()

        t = threading.Thread(
            target=main.MainWindow.sync_monitor_loop, args=(window,), daemon=True)
        t.start()
        time.sleep(0.2)  # loop is blocked inside the interruptible wait
        window.shutdown_event.set()
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())  # woke before the 30s interval elapsed


if __name__ == "__main__":
    unittest.main()
