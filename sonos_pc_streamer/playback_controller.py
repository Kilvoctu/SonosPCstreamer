"""libmpv player construction for Sonos PC Streamer.

Extracted from main.py: the create_mpv_player() factory owns every aspect
of building the libmpv player (standard kwargs, embedded-vs-detached window
setup, mpv log handler, color-output merge, best-effort cache byte-caps).
MainWindow play/stop/seek control stays in main.py -- dependency direction
is playback_controller -> logging_utils + mpv only.
"""

import mpv as _mpv

from sonos_pc_streamer.logging_utils import timestamped_print as _print


def create_mpv_player(embed_hwnd=None, extra_kwargs=None):
    """Create the libmpv player with the app's standard kwargs.

    embed_hwnd: native child-window HWND for embedded playback, or None for
    a detached window. extra_kwargs: merged last (color config etc.).
    Returns (player, embedded) — embedded is True only when embed_hwnd was
    usable. Raises on constructor failure (caller decides how to surface it).
    """
    kwargs: dict = {
        "pause": True,
        "vo": "gpu-next",
        "gpu_api": "d3d11",
        "hwdec": "auto",
        "ytdl": True,
        "ytdl_format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestaudio/best",
        "ao": "null",
        "cache": "yes",
        # Larger readahead targets high-bitrate remote 4K; byte caps are applied best-effort below
        "demuxer_readahead_secs": 30,
        "cursor_autohide": 0,
    }
    embedded = False
    if embed_hwnd:
        kwargs["wid"] = embed_hwnd
        embedded = True
        _print(f"[MPV] embed: container hwnd=0x{embed_hwnd:X}")
    if not embedded:
        kwargs.update({
            "geometry": "1280x720",
            "autofit": "1280x720",
            "title": "Sonos PC Streamer - Video",
        })

    def _mpv_log(level, prefix, text):
        if level in ("warn", "error", "fatal"):
            _print(f"[MPV:{level}] {prefix}: {text.strip()}")

    kwargs["log_handler"] = _mpv_log
    kwargs["loglevel"] = "warn"
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    player = _mpv.MPV(**kwargs)
    # Optional cache byte-caps: applied best-effort after construction
    # so an option missing from this libmpv build can never break
    # player creation (constructor kwargs fail hard on unknown options).
    for _opt_name, _opt_val in (
        ("demuxer-max-forward-bytes", "512MiB"),
        ("demuxer-max-back-bytes", "64MiB"),
    ):
        try:
            set_mpv_property(player, _opt_name, _opt_val)
        except (Exception,) as e:
            _print(f"[MPV] Option {_opt_name} set failed: {e}")
    return player, embedded


def get_mpv_property(player, name):
    """Read a raw libmpv property through the player.

    python-mpv exposes raw property I/O only via underscored methods
    (``_get_property``); hoisting them behind public functions keeps callers
    clear of the library-internal protected access.
    """
    # noinspection PyProtectedMember
    return player._get_property(name)


def set_mpv_property(player, name, value):
    """Write a raw libmpv property through the player.

    Same rationale as get_mpv_property(): the underscored python-mpv API is
    wrapped once here so playback code stays on public helpers.
    """
    # noinspection PyProtectedMember
    player._set_property(name, value)
