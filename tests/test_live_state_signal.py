"""Focused, no-network tests for resolver-originated live-state UI updates.

The daemon resolver thread inside MainWindow.do_play_uri() must never touch
widgets directly; it reports the resolved live-state through the
live_state_signal Signal(bool), which is connected to the GUI-thread slot
MainWindow.on_live_state().

Covers:

1. on_live_state() live input: disables the seek slider and sets the
   "LIVE / --:--" time text.
2. on_live_state() non-live input: enables the seek slider and never resets
   the current time text.
3. Wiring: emitting a Signal(bool) connected to the real slot reaches the
   fake receiver with correct state for both live and non-live payloads
   (direct same-thread connection, no event loop or QApplication needed).
4. Source inspection: the resolver inner function emits the signal exactly
   once and contains no direct seek_slider.setEnabled / _set_time_text
   widget calls; MainWindow declares and connects the signal.
"""

import inspect
import os
import sys
import unittest

# Make the sibling test module importable when unittest discovery imports
# this file as part of the tests package (repo root is on sys.path, not here).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QObject, Signal
from test_stream_session import _import_main_no_network


class _FakeSeekSlider:
    """Minimal stand-in for the seek slider's setEnabled() surface."""

    def __init__(self):
        self.enabled = None
        self.set_enabled_calls = 0

    def setEnabled(self, on):
        self.enabled = bool(on)
        self.set_enabled_calls += 1


class _FakeReceiver:
    """Attribute surface consumed by MainWindow.on_live_state()."""

    def __init__(self):
        self.seek_slider = _FakeSeekSlider()
        self.time_text = None

    def _set_time_text(self, text):
        self.time_text = text


class _BoolEmitter(QObject):
    """Same-shape Signal(bool) carrier to probe the real slot's wiring."""

    live_state_signal = Signal(bool)


class LiveStateSignalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_no_network()

    @staticmethod
    def _receiver():
        return _FakeReceiver()

    def test_slot_live_disables_slider_and_sets_live_text(self):
        recv = self._receiver()

        self.main.MainWindow.on_live_state(recv, True)

        self.assertFalse(recv.seek_slider.enabled)
        self.assertEqual(recv.seek_slider.set_enabled_calls, 1)
        self.assertEqual(recv.time_text, "LIVE / --:--")

    def test_slot_non_live_enables_slider_without_resetting_time_text(self):
        recv = self._receiver()
        recv.time_text = "12:34 / 56:78"  # simulates in-progress time display

        self.main.MainWindow.on_live_state(recv, False)

        self.assertTrue(recv.seek_slider.enabled)
        self.assertEqual(recv.seek_slider.set_enabled_calls, 1)
        self.assertEqual(recv.time_text, "12:34 / 56:78")  # not reset

    def test_signal_emit_reaches_real_slot_for_both_states(self):
        recv = self._receiver()
        emitter = _BoolEmitter()
        emitter.live_state_signal.connect(
            lambda is_live: self.main.MainWindow.on_live_state(recv, is_live))

        emitter.live_state_signal.emit(True)
        self.assertFalse(recv.seek_slider.enabled)
        self.assertEqual(recv.time_text, "LIVE / --:--")

        emitter.live_state_signal.emit(False)
        self.assertTrue(recv.seek_slider.enabled)
        self.assertEqual(recv.time_text, "LIVE / --:--")  # non-live left it alone

    def test_resolver_inner_uses_signal_not_direct_widget_calls(self):
        main = self.main
        self.assertTrue(hasattr(main.MainWindow, "live_state_signal"))
        init_src = inspect.getsource(main.MainWindow.__init__)
        self.assertIn("self.live_state_signal.connect(self.on_live_state)", init_src)

        src = inspect.getsource(main.MainWindow.do_play_uri)
        start = src.index("def _resolve_and_play_inner():")
        end = src.index("def resolve_and_play():")
        inner = src[start:end]

        self.assertNotIn("self.seek_slider.setEnabled", inner)
        self.assertNotIn("self._set_time_text(", inner)
        self.assertEqual(inner.count("self.live_state_signal.emit"), 1)
        self.assertIn("self.live_state_signal.emit(is_live)", inner)


if __name__ == "__main__":
    unittest.main()
