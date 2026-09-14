"""Deterministic, no-network probes for the serialized Sonos volume worker.

MainWindow.on_volume_changed() only updates the UI and then hands the value
to queue_speaker_volume(); the actual speaker write is owned by a single
instance-backed worker thread (MainWindow.volume_worker) which stages the
newest pending value under a lock and sends it after a short debounce.
main.py is imported with soco/mpv/yt_dlp stubbed out (same harness as
test_stream_session.py) and main.speaker is swapped for a recording fake, so
these probes prove for rapid slider values:

1. A tight burst (10 -> 20 -> 30 inside the debounce window) is coalesced
   into a single write of the newest value (30).
2. Values queued while a write operation is in flight are superseded in
   order; the intermediate value is never sent (a blocked 10-write
   completes, then exactly one more write applies 30). At most one write is
   in flight at a time, and every write comes from the single worker thread.
3. Shutdown (shutdown_event) stops the worker before it writes and makes
   later slider events silent no-ops.
4. The initial Sonos volume fetch (on_sonos_volume, via _sonos_vol_signal)
   applies the fetched value to both sliders with signals blocked: no
   valueChanged feedback, no user-touch flag, and no queued speaker write.
"""

import threading
import time
import unittest

from _import_main import import_main_no_network as _import_main_no_network


class _RecordingSpeaker:
    """Fake SoCo speaker: records volume writes and detects overlapping ones."""

    def __init__(self, gate_first_write=False):
        self._lock = threading.Lock()
        self._writes = []
        self._writer_idents = set()
        self._active_writes = 0
        self.max_concurrent_writes = 0
        self._gate_first_write = gate_first_write
        self.first_write_entered = threading.Event()
        self.release_first_write = threading.Event()

    @property
    def writes(self):
        with self._lock:
            return list(self._writes)

    @property
    def writer_idents(self):
        with self._lock:
            return set(self._writer_idents)

    @property
    def volume(self):
        with self._lock:
            return self._writes[-1] if self._writes else None

    @volume.setter
    def volume(self, val):
        with self._lock:
            gate = self._gate_first_write and not self._writes
            self._active_writes += 1
            self.max_concurrent_writes = max(
                self.max_concurrent_writes, self._active_writes)
        try:
            if gate:  # optional: park the first write mid-flight
                self.first_write_entered.set()
                self.release_first_write.wait(timeout=10)
            with self._lock:
                self._writes.append(val)
                self._writer_idents.add(threading.get_ident())
        finally:
            with self._lock:
                self._active_writes -= 1


class _StubVolumeWindow:
    """Attribute surface consumed by queue_speaker_volume/volume_worker."""

    def __init__(self, debounce_sec):
        self.VOLUME_DEBOUNCE_SEC = debounce_sec
        self.shutdown_event = threading.Event()
        self.vol_lock = threading.Lock()
        self.pending_volume = None
        self.vol_event = threading.Event()


class _FakeSlider:
    """Minimal QSlider stand-in: setValue() notifies connected listeners with
    the new value unless signals are blocked (mirrors QSlider.valueChanged
    under blockSignals, but fires on every unblocked setValue — stricter than
    Qt, which only emits on an actual change — so a missing blockSignals() is
    caught even when the fetched volume equals the slider's current value)."""

    def __init__(self, value):
        self.value = value
        self._signals_blocked = False
        self.listeners = []

    def blockSignals(self, blocked):
        previous = self._signals_blocked
        self._signals_blocked = blocked
        return previous

    def setValue(self, value):
        self.value = value
        if not self._signals_blocked:
            for listener in self.listeners:
                listener(value)

    @property
    def signals_blocked(self):
        return self._signals_blocked


class _FakeLabel:
    """Minimal QLabel stand-in recording the last setText payload."""

    def __init__(self):
        self.text = None

    def setText(self, text):
        self.text = text


class _VolumeUiWindow(_StubVolumeWindow):
    """Adds the slider/label surface consumed by on_sonos_volume.

    Each fake-slider listener stands in for the real
    valueChanged -> on_volume_changed connection: any entry recorded in
    signal_feedback means the initial fetch leaked a signal.
    """

    def __init__(self, debounce_sec):
        super().__init__(debounce_sec)
        self.user_volume_touched = False
        self.vol_slider = _FakeSlider(30)
        self.fs_vol_slider = _FakeSlider(30)
        self.vol_label = _FakeLabel()
        self.signal_feedback = []
        self.vol_slider.listeners.append(
            lambda val: self.signal_feedback.append(("vol_slider", val)))
        self.fs_vol_slider.listeners.append(
            lambda val: self.signal_feedback.append(("fs_vol_slider", val)))


class SonosVolumeWorkerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = _import_main_no_network()

    def _make_window(self):
        return _StubVolumeWindow(self.main.MainWindow.VOLUME_DEBOUNCE_SEC)

    def _start_worker(self, window):
        worker = threading.Thread(
            target=self.main.MainWindow.volume_worker, args=(window,), daemon=True)
        worker.start()
        return worker

    @staticmethod
    def _stop_worker(window, worker):
        # Mirror closeEvent: flag shutdown, then wake the (possibly idle)
        # worker so it observes the flag instead of sleeping forever.
        window.shutdown_event.set()
        window.vol_event.set()
        worker.join(timeout=5.0)
        return not worker.is_alive()

    def test_rapid_burst_coalesces_to_single_latest_write(self):
        main = self.main
        original_speaker = main.speaker
        speaker = _RecordingSpeaker()
        main.speaker = speaker
        window = self._make_window()
        worker = self._start_worker(window)
        try:
            # Rapid drag: all three events land far inside the debounce
            # window (microseconds apart vs. the 150ms debounce), so only
            # the newest value may reach the speaker, exactly once.
            for val in (10, 20, 30):
                main.MainWindow.queue_speaker_volume(window, val)

            deadline = time.time() + 5
            while time.time() < deadline and not speaker.writes:
                time.sleep(0.01)
            self.assertEqual(speaker.writes, [30])
            self.assertEqual(speaker.volume, 30)
        finally:
            self.assertTrue(self._stop_worker(window, worker))
            main.speaker = original_speaker

        # No overlapping writes and no per-event threads: every write came
        # from the single worker thread.
        self.assertEqual(speaker.max_concurrent_writes, 1)
        self.assertEqual(speaker.writer_idents, {worker.ident})

    def test_values_queued_during_write_are_superseded_in_order(self):
        main = self.main
        original_speaker = main.speaker
        speaker = _RecordingSpeaker(gate_first_write=True)
        main.speaker = speaker
        window = self._make_window()
        worker = self._start_worker(window)
        try:
            main.MainWindow.queue_speaker_volume(window, 10)
            # The worker drains 10 and parks inside the (gated) Sonos write.
            self.assertTrue(speaker.first_write_entered.wait(timeout=5))

            # 20 and 30 arrive while that write is still in flight: when the
            # worker next runs it can only take the newest staged value.
            main.MainWindow.queue_speaker_volume(window, 20)
            main.MainWindow.queue_speaker_volume(window, 30)
            speaker.release_first_write.set()

            deadline = time.time() + 5
            while time.time() < deadline and len(speaker.writes) < 2:
                time.sleep(0.01)
            self.assertEqual(speaker.writes, [10, 30])
        finally:
            self.assertTrue(self._stop_worker(window, worker))
            main.speaker = original_speaker

        self.assertEqual(speaker.max_concurrent_writes, 1)
        self.assertEqual(speaker.writer_idents, {worker.ident})

    def test_shutdown_stops_worker_and_blocks_new_writes(self):
        main = self.main
        original_speaker = main.speaker
        speaker = _RecordingSpeaker()
        main.speaker = speaker
        window = self._make_window()
        try:
            # An idle worker woken by the closeEvent-style shutdown wake
            # (shutdown flag + event set) must exit without writing.
            worker = self._start_worker(window)
            time.sleep(0.05)  # let it reach its idle wait
            window.shutdown_event.set()
            window.vol_event.set()
            worker.join(timeout=5.0)
            self.assertFalse(worker.is_alive())

            # After shutdown, slider events must be silent no-ops: nothing
            # staged, nothing written, and no worker anywhere to write.
            main.MainWindow.queue_speaker_volume(window, 55)
            self.assertIsNone(window.pending_volume)
            self.assertEqual(speaker.writes, [])
        finally:
            main.speaker = original_speaker

    def test_initial_sonos_volume_sync_updates_both_sliders_without_feedback(self):
        main = self.main
        original_speaker = main.speaker
        speaker = _RecordingSpeaker()
        main.speaker = speaker
        window = _VolumeUiWindow(main.MainWindow.VOLUME_DEBOUNCE_SEC)
        worker = self._start_worker(window)
        try:
            # The one-shot reader publishes the speaker's own volume; the
            # slot must apply it as a silent programmatic sync (42 differs
            # from the sliders' 30 default, so a real change is involved).
            main.MainWindow.on_sonos_volume(window, 42)

            # Both sliders show the fetched value and signals were restored.
            self.assertEqual(window.vol_slider.value, 42)
            self.assertEqual(window.fs_vol_slider.value, 42)
            self.assertFalse(window.vol_slider.signals_blocked)
            self.assertFalse(window.fs_vol_slider.signals_blocked)
            self.assertEqual(window.vol_label.text, "Volume: 42")

            # No signal feedback: the listeners standing in for the real
            # valueChanged -> on_volume_changed connection never fired, so
            # the fetch was not treated as a user touch and nothing was
            # staged for the speaker.
            self.assertEqual(window.signal_feedback, [])
            self.assertFalse(window.user_volume_touched)
            self.assertIsNone(window.pending_volume)
            self.assertFalse(window.vol_event.is_set())

            # Even with the worker alive through the full debounce window,
            # the initial read must produce zero speaker writes.
            time.sleep(main.MainWindow.VOLUME_DEBOUNCE_SEC + 0.25)
            self.assertEqual(speaker.writes, [])
        finally:
            self.assertTrue(self._stop_worker(window, worker))
            main.speaker = original_speaker


if __name__ == "__main__":
    unittest.main()
