"""Shared test harness: import main.py with device/network-facing stubs.

Several no-network test modules (test_stream_session.py,
test_video_format_policy.py, test_volume_worker.py) need main.py loaded with
soco/mpv/yt_dlp fake modules and the local-IP UDP probe short-circuited, but
never touch a socket.  Keeping that bootstrap in one module avoids the
identical copy that used to live in each test file.

The import side effect is confined: stubs are removed from sys.modules again
and ``socket.socket.connect`` is restored before returning.
"""

import importlib
import socket
import sys
import types


def import_main_no_network():
    """Import main.py with device/network-facing dependencies stubbed out."""
    soco_stub = types.ModuleType("soco")

    class SoCo:  # matches the real class name main.py instantiates
        def __init__(self, *args, **kwargs):
            pass

    soco_stub.SoCo = SoCo
    stubs = {
        "soco": soco_stub,
        "mpv": types.ModuleType("mpv"),
        "yt_dlp": types.ModuleType("yt_dlp"),
    }
    saved_modules = {name: sys.modules.get(name) for name in stubs}
    saved_connect = socket.socket.connect

    def _fake_connect(sock, _address):
        # Stand in for get_local_ip()'s UDP "connect" without any network
        # traffic; bind a wildcard local address so getsockname() still works.
        try:
            sock.bind(("0.0.0.0", 0))
        except OSError:
            pass

    socket.socket.connect = _fake_connect
    try:
        sys.modules.update(stubs)
        main = importlib.import_module("main")
    finally:
        socket.socket.connect = saved_connect
        for name, saved in saved_modules.items():
            if saved is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved
    return main