"""Sonos<->mpv sync monitor for Sonos PC Streamer.

Extracted from main.py: the Sonos position poll loop, drift correction and
re-anchoring, offset/rate learning, the Sonos-stream restart at the current
position, the parse_sonos_position helper, and the seek_base_pos anchor
(canonical home; main.py reaches it via get/set_seek_base_pos). The moved
functions take the window as their first parameter and touch mpv/Qt state
through it. UI-free on purpose (no Qt, no mpv, no yt_dlp) -- dependency
direction is sync_controller -> sonos_controller/stream_server/logging_utils
only.
"""

import time

from sonos_pc_streamer.logging_utils import timestamped_print as _print
from sonos_pc_streamer.sonos_controller import sonos_lock, speaker, start_sonos_stream
from sonos_pc_streamer.stream_server import set_audio_stream

seek_base_pos = 0.0


def get_seek_base_pos():
    return seek_base_pos


def set_seek_base_pos(value):
    global seek_base_pos
    seek_base_pos = float(value)


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
    except (Exception,) as e:
        _print(f"[SYNC] Position parse failed: {e}")
    return 0.0


def run_monitor_loop(w):
    stall_streak = 0
    while True:
        # Interruptible tick: wakes early (and exits) the moment
        # w.shutdown_event is set instead of sleeping a full interval.
        if w.shutdown_event.wait(w.SONOS_POLL_INTERVAL):
            return
        if not w.sync_ready or w.seeking or w.seek_in_progress:
            continue
        try:
            with sonos_lock:
                transport = speaker.get_current_transport_info()
            state = transport.get("current_transport_state", "")
        except (Exception,) as e:
            _print(f"[SYNC] Transport poll failed: {e}")
            continue  # transient SoCo/network error — skip tick, never restart on it
        if state not in ("PLAYING", "TRANSITIONING"):
            stall_streak += 1
            if stall_streak >= w.SONOS_STALL_TICKS:
                stall_streak = 0
                w.restart_sonos_at_current_pos()
            continue
        stall_streak = 0
        if state != "PLAYING":
            continue  # TRANSITIONING — no correction while Sonos switches
        now = time.time()
        prev_poll = w.last_poll_time
        w.last_poll_time = now
        try:
            with sonos_lock:
                info = speaker.get_current_track_info()
            pos_str = info.get('position', '')
            if not pos_str or pos_str == 'NOT_IMPLEMENTED':
                continue
            elapsed = parse_sonos_position(pos_str)
            if elapsed <= 0:
                continue
            if elapsed != w.last_soco_elapsed:
                # Estimate the true tick moment as the midpoint between the
                # previous poll (old value) and this one (new value) — halves
                # the extrapolation sawtooth vs. stamping "now".
                if prev_poll > 0 and (now - prev_poll) <= 1.0:
                    w.last_soco_time = (prev_poll + now) / 2.0
                else:
                    w.last_soco_time = now
                w.last_soco_elapsed = elapsed
            stalled = (now - w.last_soco_time) > w.SONOS_STALL_SECS and now >= w.correction_holdoff_until
            if stalled and not w.stall_active:
                w.stall_active = True
                w.stall_started_at = now
                _print("[SYNC] Sonos stalled — holding video frame")
            elif not stalled and w.stall_active:
                # First fresh reading after a stall: re-base cleanly.
                w.stall_active = False
                w.drift_hist = []
                w.fd_samples = []
                w.correction_holdoff_until = now + w.REANCHOR_SETTLE
                _print("[SYNC] Sonos recovered — resuming video")
                with w.av_lock:
                    if w.player:
                        try:
                            w.player.pause = False
                        except (Exception,) as e:
                            _print(f"[SYNC] Resume (recovered) failed: {e}")
                continue
            if stalled:
                if (now - w.stall_started_at) > w.SONOS_STALL_RESTART_AFTER:
                    _print(f"[SYNC] Sonos stalled >{w.SONOS_STALL_RESTART_AFTER:.0f}s — forcing stream restart")
                    w.stall_active = False
                    w.last_soco_time = now  # restart at last KNOWN position, not extrapolated
                    w.restart_sonos_at_current_pos()
                    continue
                with w.av_lock:
                    if w.player:
                        try:
                            w.player.pause = True
                        except (Exception,) as e:
                            _print(f"[SYNC] Pause (stall) failed: {e}")
                continue
            time_since = min(now - w.last_soco_time, w.EXTRAP_CAP)
            audio_pos = seek_base_pos + w.last_soco_elapsed + time_since - w.user_sync_offset - w.sync_offset_runtime
            if audio_pos < 0:
                audio_pos = 0.0
            w.sonos_pos_signal.emit(pos_str, audio_pos)
        except (Exception,) as e:
            _print(f"[SYNC] Poll error: {e}")
            continue
        if not w.player:
            continue
        try:
            buffering = w.read_mpv_property("paused-for-cache")
        except (Exception,):
            buffering = False
        if buffering:
            if not w.mpv_buffering:
                w.mpv_buffering = True
                _print("[SYNC] mpv buffering (demuxer underrun) — holding corrections")
                w.buffer_episodes += 1
                _print(f"[SYNC] mpv buffering episode {w.buffer_episodes} this play")
                # Recurring underruns: request a tier downgrade via signal —
                # this daemon thread must not call do_play_uri directly.
                if (w.buffer_episodes >= 3
                        and (now - w.last_downgrade_at) >= 120.0
                        and w.current_video_tier_index is not None):
                    w.last_downgrade_at = now
                    _print("[SYNC] 3+ buffering episodes — requesting video tier downgrade")
                    w.downgrade_signal.emit()
            continue  # drift measured during a stall is an artifact — skip
        if w.mpv_buffering:
            w.mpv_buffering = False
            w.drift_hist = []
            w.fd_samples = []
            w.initial_sync_done = False
            w.correction_holdoff_until = time.time() + 2.0
            _print("[SYNC] mpv buffer recovered — re-syncing")
        try:
            video_pos = w.player.time_pos
        except (Exception,) as e:
            _print(f"[SYNC] time_pos read failed: {e}")
            continue
        if video_pos is None:
            continue
        fd = video_pos - audio_pos
        w.fd_samples.append((now, fd))
        if len(w.fd_samples) > 600:
            w.fd_samples.pop(0)
        w.learn_from_drift(now)
        w.apply_drift_correction(fd, audio_pos)


def restart_sonos_at_current_pos(w):
    global seek_base_pos
    if w.seek_in_progress:
        return
    audio_url = getattr(w, '_resolved_audio_url', None)
    if not (w.current_uri and w.player and audio_url):
        return
    audio_pos = seek_base_pos + w.last_soco_elapsed + min(
        time.time() - w.last_soco_time, w.EXTRAP_CAP)
    if audio_pos < 0:
        audio_pos = 0.0
    _print(f"[SYNC] Sonos stopped — restarting stream at {audio_pos:.1f}s")
    set_audio_stream(audio_url, seek_sec=audio_pos,
                     headers=getattr(w, '_resolved_audio_headers', {}))
    seek_base_pos = audio_pos
    w.last_soco_elapsed = 0.0
    w.last_soco_time = time.time()
    w.initial_sync_done = False
    w.reset_sync_state()
    start_sonos_stream()


def _hard_reseek(w, audio_pos, error_label):
    """Pause → absolute seek → unpause → set base speed.

    Returns the base speed on success, or None on failure (error logged).
    Used by both the initial sync and the re-anchor path in
    apply_drift_correction(); the two used to be duplicated blocks.
    """
    base = 1.0 - float(w.sync_rate_bias)
    try:
        w.player.pause = True
        w.player.seek(audio_pos, "absolute")
        w.player.pause = False
        w.player.speed = base
    except (Exception,) as exc:
        _print(f"[SYNC] {error_label}: {exc}")
        return None
    w.current_speed = base
    return base


def apply_drift_correction(w, drift, audio_pos):
    audio_pos = float(audio_pos)
    now = time.time()
    if now < w.correction_holdoff_until:
        return  # seek/settle window — measurements taken now are unreliable
    with w.av_lock:
        if not w.player:
            return
        if not w.initial_sync_done:
            base = _hard_reseek(w, audio_pos, "Init sync error")
            if base is None:
                return
            w.drift_hist = []
            w.drift_f = drift
            w.last_resync = now
            w.initial_sync_done = True
            w.wait_for_mpv_seek(audio_pos)
            w.correction_holdoff_until = time.time() + w.REANCHOR_SETTLE
            _print(f"[SYNC] Init sync: drift={drift:.2f}s → {audio_pos:.1f}s")
            return
        w.drift_hist.append(drift)
        if len(w.drift_hist) > 7:
            w.drift_hist.pop(0)
        med = sorted(w.drift_hist)[len(w.drift_hist) // 2]
        w.drift_f = 0.65 * w.drift_f + 0.35 * med
        fd = float(w.drift_f)
        reanchor_allowed = (now - w.last_resync) > w.REANCHOR_THROTTLE
        if w.play_started_at and (now - w.play_started_at) < w.PLAY_WARMUP_GRACE:
            reanchor_allowed = False  # decode warmup: let the speed corrector catch up, don't cut
        if abs(fd) > w.DRIFT_REANCHOR and reanchor_allowed:
            base = _hard_reseek(w, audio_pos, "Re-anchor error")
            if base is None:
                return
            w.drift_f = 0.0
            w.drift_hist = []
            w.corr_engaged = False
            w.last_resync = now
            w.wait_for_mpv_seek(audio_pos)
            w.correction_holdoff_until = time.time() + w.REANCHOR_SETTLE
            _print(f"[SYNC] Re-anchor: drift={fd:.2f}s → {audio_pos:.1f}s")
            return
        if w.corr_engaged and abs(fd) <= w.DRIFT_RELEASE:
            w.corr_engaged = False
        if not w.corr_engaged:
            if abs(fd) <= w.DRIFT_ENGAGE:
                base = 1.0 - w.sync_rate_bias
                if abs(w.current_speed - base) > 1e-6:
                    try:
                        w.player.speed = base
                    except (Exception,) as e:
                        _print(f"[SYNC] Speed write failed: {e}")
                    w.current_speed = base
                return
            w.corr_engaged = True
            w.last_engage_time = now
        base = 1.0 - w.sync_rate_bias
        if abs(fd) <= w.DRIFT_FINE:
            spd = base - fd / w.SPEED_FINE_DIV
            clamp = w.SPEED_FINE_CLAMP
        else:
            spd = base - fd / w.SPEED_COARSE_DIV
            clamp = w.SPEED_COARSE_CLAMP
        spd = float(max(base - clamp, min(base + clamp, spd)))
        if abs(spd - w.current_speed) > 0.005 and (now - w.last_speed_write) >= w.SPEED_CHANGE_MIN_INTERVAL:
            try:
                w.player.speed = spd
            except (Exception,) as e:
                _print(f"[SYNC] Speed write failed: {e}")
            w.current_speed = spd
            w.last_speed_write = now
            w.corrections_count += 1
            _print(f"[SYNC] Speed: drift={fd:.3f}s → {spd:.3f}x")


def learn_from_drift(w, now):
    # Telemetry first — prints even while corrections are active.
    if w.fd_samples and (now - w.last_telemetry) >= w.TELEMETRY_INTERVAL:
        w.last_telemetry = now
        vals = [fd for _, fd in w.fd_samples]
        med = float(sorted(vals)[len(vals) // 2])
        _print(f"[SYNC] drift: median={med:+.3f}s rate_bias={float(w.sync_rate_bias) * 1e6:+.0f}ppm "
              f"offset={float(w.sync_offset_runtime):+.3f}s engaged={w.corr_engaged} corr={w.corrections_count}/60s")
        w.corrections_count = 0
    # Learning only during quiet periods (no correction in the window).
    if w.last_engage_time and (now - w.last_engage_time) < w.LEARN_QUIET:
        return
    if now < w.correction_holdoff_until:
        return
    clean = [(t, fd) for (t, fd) in w.fd_samples if t >= now - w.LEARN_QUIET]
    if len(clean) < 150:
        return
    if now - clean[0][0] < w.LEARN_MIN_WINDOW:
        return
    vals = [fd for _, fd in clean]
    spread = max(vals) - min(vals)
    med = float(sorted(vals)[len(vals) // 2])

    # 1) Anchor offset calibration: a stable nonzero median is a constant
    #    bias (Sonos buffer/report offset), not drift — trim it out once.
    if spread <= w.OFFSET_SPREAD_MAX and abs(med) > w.OFFSET_TRIM_MIN:
        w.sync_offset_runtime -= med
        w.fd_samples = []
        _print(f"[SYNC] Offset trim: {med:+.3f}s → runtime offset {-float(w.sync_offset_runtime):+.3f}s")
        return

    # 2) Rate feed-forward: early-vs-late window medians estimate the drift
    #    slope (Sonos clock vs PC clock). Apply as a constant base speed.
    n = len(clean)
    third = n // 3
    early = sorted(vals[:third])
    late = sorted(vals[-third:])
    m_e = float(early[len(early) // 2])
    m_l = float(late[len(late) // 2])
    t_e = float(clean[third // 2][0])
    t_l = float(clean[n - 1 - third // 2][0])
    dt = t_l - t_e
    if dt < 30.0:
        return
    slope = (m_l - m_e) / dt
    if abs(slope) * 1e6 < w.RATE_MIN_PPM:
        return
    if abs(slope) > w.RATE_MAX_PPM * 1e-6:
        _print(f"[SYNC] Rate estimate out of range ({slope * 1e6:+.0f}ppm) — ignoring")
        w.fd_samples = []
        return
    w.sync_rate_bias = max(-w.RATE_BIAS_CLAMP,
                            min(w.RATE_BIAS_CLAMP, w.sync_rate_bias + slope))
    w.fd_samples = []
    base = 1.0 - float(w.sync_rate_bias)
    with w.av_lock:
        if w.player and abs(w.current_speed - base) > 1e-6:
            try:
                w.player.speed = base
            except (Exception,) as e:
                _print(f"[SYNC] Speed write failed: {e}")
            w.current_speed = base
            w.corrections_count += 1
    _print(f"[SYNC] Rate match: {slope * 1e6:+.0f}ppm → base speed {base:.5f}")


def reset_sync_state(w):
    w.drift_f = 0.0
    w.drift_hist = []
    w.last_resync = 0.0
    w.corr_engaged = False
