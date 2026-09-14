"""Focused, no-network tests for the video startup health gate.

Covers:

1. video_health_verdict() threshold rules (pure function, no mpv):
   4K/2160 requires active hardware decode, lower tiers tolerate software
   decode, sustained frame drops and dominant paused-for-cache stalls are
   unhealthy, and the first matching rule wins.
2. Source inspection of the resolver play block in
   MainWindow.do_play_uri._resolve_and_play_inner(): the health-gated
   candidate loop and legacy fallthrough are present, exactly one
   _video_load_fallback spawn exists, and set_audio_stream() is reached
   only after the winner/color block.
3. The recurring-underrun downgrade trigger in sync_monitor_loop(),
   driven against a stub window (no mpv/Sonos/GUI): the buffering-episode
   counter increments per buffering entry, and the tier-downgrade request
   fires only when threshold (>=3 episodes), cooldown (>=120s) and an
   active video tier all pass; the counter resets in do_stop() and after
   the downgrade guards pass.

Nothing touches the network, mpv, Sonos, or a GUI.
"""

import inspect
import os
import sys
import time
import types
import unittest

from sonos_pc_streamer import sync_controller

# Make the sibling test module importable when unittest discovery imports
# this file as part of the tests package (repo root is on sys.path, not here).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_stream_session import _import_main_no_network


class VideoHealthVerdictTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_no_network()

    # -- 4K hardware-decode rule ------------------------------------------

    def test_4k_without_hwdec_unhealthy(self):
        for hwdec in (None, "", "no"):
            with self.subTest(hwdec=hwdec):
                ok, reason = self.main.video_health_verdict(hwdec, "2160", 0, 0.0)
                self.assertFalse(ok)
                self.assertEqual(reason, "no hardware decode for 4K")

    def test_4k_with_hwdec_healthy(self):
        for hwdec in ("d3d11va", "nvdec", "auto-safe"):
            with self.subTest(hwdec=hwdec):
                ok, reason = self.main.video_health_verdict(hwdec, "2160", 0, 0.0)
                self.assertTrue(ok)
                self.assertEqual(reason, "")

    def test_1080p_software_decode_healthy(self):
        ok, reason = self.main.video_health_verdict(None, "1080", 0, 0.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    # -- frame-drop rule ---------------------------------------------------

    def test_frame_drop_boundary_45_ok_46_unhealthy(self):
        ok, _ = self.main.video_health_verdict("d3d11va", "1080", 45, 0.0)
        self.assertTrue(ok)
        ok, reason = self.main.video_health_verdict("d3d11va", "1080", 46, 0.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "sustained frame drops")

    # -- cache-underrun rule -----------------------------------------------

    def test_cache_ratio_boundary_half_ok_above_unhealthy(self):
        ok, _ = self.main.video_health_verdict("d3d11va", "1080", 0, 0.5)
        self.assertTrue(ok)
        ok, reason = self.main.video_health_verdict("d3d11va", "1080", 0, 0.51)
        self.assertFalse(ok)
        self.assertEqual(reason, "repeated cache underruns")

    # -- rule precedence ----------------------------------------------------

    def test_first_match_wins_precedence(self):
        # Software-decoded 4K with drops AND cache stalls reports the 4K rule.
        ok, reason = self.main.video_health_verdict(None, "2160", 100, 1.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "no hardware decode for 4K")
        # Hardware-decoded tier with drops AND cache stalls reports the
        # drop rule (it precedes the cache rule).
        ok, reason = self.main.video_health_verdict("nvdec", "1080", 100, 1.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "sustained frame drops")


class ResolverPlayBlockSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_no_network()

    def _resolver_slice(self):
        src = inspect.getsource(self.main.MainWindow.do_play_uri)
        start = src.index("def _resolve_and_play_inner():")
        end = src.index("def resolve_and_play():")
        return src[start:end]

    def test_health_gated_candidate_loop_and_fallthrough_present(self):
        inner = self._resolver_slice()
        self.assertIn("for cand_idx, cand in enumerate(video_candidates):", inner)
        self.assertIn("self._check_video_startup_health(", inner)
        self.assertIn("if winner is None:", inner)

    def test_exactly_one_video_load_fallback_spawn(self):
        inner = self._resolver_slice()
        self.assertEqual(
            inner.count(
                "threading.Thread(target=self._video_load_fallback, "
                "args=(uri, generation), daemon=True).start()"),
            1)

    def test_set_audio_stream_after_winner_color_block(self):
        inner = self._resolver_slice()
        audio_idx = inner.index("set_audio_stream(")
        block = inner[:audio_idx]
        self.assertIn("if winner is not None:", block)
        self.assertIn("self._source_is_hdr = winner.is_hdr", block)
        self.assertIn("self._configure_color_output()", block)
        self.assertIn("self._schedule_color_log.emit()", block)


class DowngradeTriggerLoopTest(unittest.TestCase):
    """Runs the real sync_monitor_loop body against a stub window.

    The loop is a daemon thread in production and cannot call do_play_uri
    directly; the trigger hops to the GUI thread via downgrade_signal.
    Here the loop method is invoked directly on a duck-typed stub so the
    buffering-episode counting and the trigger gate are exercised as
    written, without mpv, Sonos, or Qt.
    """

    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_no_network()

    def setUp(self):
        self._saved_speaker = sync_controller.speaker

    def tearDown(self):
        sync_controller.speaker = self._saved_speaker

    def _make_stub(self):
        MW = self.main.MainWindow
        stub = types.SimpleNamespace()
        stub.SONOS_POLL_INTERVAL = MW.SONOS_POLL_INTERVAL
        stub.SONOS_STALL_SECS = MW.SONOS_STALL_SECS
        stub.SONOS_STALL_TICKS = MW.SONOS_STALL_TICKS
        stub.EXTRAP_CAP = MW.EXTRAP_CAP
        stub.sync_ready = True
        stub.seeking = False
        stub.seek_in_progress = False
        stub.last_poll_time = 0.0
        stub.last_soco_elapsed = 0.0
        stub.last_soco_time = 0.0
        stub.correction_holdoff_until = 0.0
        stub.stall_active = False
        stub.stall_started_at = 0.0
        stub.user_sync_offset = 0.0
        stub.sync_offset_runtime = 0.0
        stub.mpv_buffering = False
        stub.drift_hist = []
        stub.fd_samples = []
        stub.initial_sync_done = False
        stub.buffer_episodes = 0
        stub.last_downgrade_at = 0.0
        stub.current_video_tier_index = 0
        stub.sonos_pos_signal = types.SimpleNamespace(emit=lambda *a: None)
        emits = []
        stub.downgrade_signal = types.SimpleNamespace(emit=lambda: emits.append(1))
        stub.emits = emits

        class _Player:
            def __init__(self):
                self.calls = 0
                self.time_pos = None  # recovery ticks bail out via `is None`

            def _get_property(self, _name):
                self.calls += 1
                return self.calls % 2 == 1  # odd calls = buffering entry

        stub.player = _Player()
        stub.read_mpv_property = stub.player._get_property

        class _Speaker:
            def __init__(self):
                self.ticks = 0

            @staticmethod
            def get_current_transport_info():
                return {"current_transport_state": "PLAYING"}

            def get_current_track_info(self):
                self.ticks += 1
                # noinspection PyStringConversionWithoutDunderMethod
                return {"position": str(100 + self.ticks)}  # always advances

        sync_controller.speaker = _Speaker()
        return stub

    def _run_ticks(self, stub, ticks):
        class _Event:
            def __init__(self, left):
                self.left = left

            def wait(self, _timeout=None):
                if self.left > 0:
                    self.left -= 1
                    return False
                return True  # loop exits cleanly

        stub.shutdown_event = _Event(ticks)
        self.main.MainWindow.sync_monitor_loop(stub)

    def test_episode_counter_and_trigger_gating(self):
        stub = self._make_stub()
        # Episodes 1-3 (entry+recovery pairs): only ep3 crosses threshold.
        self._run_ticks(stub, 6)
        self.assertEqual(len(stub.emits), 1)
        self.assertGreater(stub.last_downgrade_at, 0.0)
        # Episodes 4-6: threshold met but cooldown blocks (no new emit).
        self._run_ticks(stub, 6)
        self.assertEqual(len(stub.emits), 1)
        # Cooldown expired: next episode fires again.
        stub.last_downgrade_at = time.time() - 121.0
        self._run_ticks(stub, 2)
        self.assertEqual(len(stub.emits), 2)
        # No active tier: gate blocks even with threshold + cooldown met.
        stub.current_video_tier_index = None
        stub.last_downgrade_at = time.time() - 121.0
        self._run_ticks(stub, 2)
        self.assertEqual(len(stub.emits), 2)
        # Tier present again: trigger fires once more.
        stub.current_video_tier_index = 0
        stub.last_downgrade_at = time.time() - 121.0
        self._run_ticks(stub, 2)
        self.assertEqual(len(stub.emits), 3)
        self.assertEqual(stub.buffer_episodes, 9)
        self.assertEqual(stub.player.calls, 18)

    def test_counter_resets_and_signal_hop_wired(self):
        init_src = inspect.getsource(self.main.MainWindow.__init__)
        self.assertIn("self.downgrade_signal.connect(self.downgrade_video_tier)", init_src)
        stop_src = inspect.getsource(self.main.MainWindow.do_stop)
        self.assertIn("self.buffer_episodes = 0", stop_src)
        downgrade_src = inspect.getsource(self.main.MainWindow.downgrade_video_tier)
        self.assertIn("self.buffer_episodes = 0", downgrade_src)


if __name__ == "__main__":
    unittest.main()
