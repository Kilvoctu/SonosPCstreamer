import ctypes
import os
import sys
import threading
import time
import traceback
from ctypes import wintypes

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QFont, QGuiApplication, QMouseEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from sonos_pc_streamer import media, playback_controller, stream_server, sync_controller
from sonos_pc_streamer.config import (
    SYNC_OFFSET_SECONDS,
    boost_process_priority,
    free_port,
    load_settings,
    local_ip,
    require_ffmpeg,
    save_settings,
    sonos_ip,
    stream_port,
)
from sonos_pc_streamer.logging_utils import timestamped_print as _print
from sonos_pc_streamer.media import (
    describe_video_format_candidate,  # noqa: F401 — re-exported for the test suite
    format_is_hdr,  # noqa: F401 — re-exported for the test suite
    probe_audio_tracks,
    rank_video_format_candidates,  # noqa: F401 — re-exported for the test suite
    resolve_media_url,
    scrape_page_for_media,
    video_health_verdict,
    yt_error_text,
)

# Sonos device control lives in sonos_pc_streamer.sonos_controller; these
# re-exports keep the main.py call sites and tests that reference these
# names working unchanged (speaker and sonos_lock are the shared objects
# the controller functions use).
from sonos_pc_streamer.sonos_controller import (
    sonos_lock,
    speaker,
    start_sonos_stream,
    stop_sonos_stream,
    wait_for_sonos_audio,
)
from sonos_pc_streamer.ui.style import DARK_STYLE
from sonos_pc_streamer.ui.video_frame import VideoFrame

_DWMWA_WINDOW_CORNER_PREFERENCE = 33

# Stream/HTTP/ffmpeg machinery lives in sonos_pc_streamer.stream_server;
# these function-object re-exports keep the main.py call sites and tests
# that reference these names working unchanged.
StreamHandler = stream_server.StreamHandler
StreamSession = stream_server.StreamSession
active_stream_stderr_tail = stream_server.active_stream_stderr_tail
kill_ffmpeg_process = stream_server.kill_ffmpeg_process
shutdown_stream_server = stream_server.shutdown_stream_server
stop_active_stream_session = stream_server.stop_active_stream_session
set_audio_stream = stream_server.set_audio_stream
start_stream_server = stream_server.start_stream_server


def fmt_time(ms):
    ms = max(ms, 0)
    total_sec = ms // 1000
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    s = total_sec % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class MainWindow(QMainWindow):
    _status_signal = Signal(str)
    _schedule_color_log = Signal()
    sonos_pos_signal = Signal(str, float)
    _probe_signal = Signal(object)
    _playback_end_signal = Signal(str)
    _video_reconfig_signal = Signal()
    _fs_menu_signal = Signal()
    _sonos_vol_signal = Signal(int)
    live_state_signal = Signal(bool)
    downgrade_signal = Signal()

    # --- Sync tuning constants (safe to adjust) ---
    SONOS_POLL_INTERVAL = 0.2        # monitor loop tick (seconds)
    SONOS_STALL_TICKS = 8            # consecutive non-playing ticks before auto-restart (~2s)
    EXTRAP_CAP = 2.0                 # max seconds to extrapolate past the last Sonos reading
    DRIFT_DEADBAND = 0.05            # |drift| <= this -> fully disengaged, speed 1.0
    DRIFT_ENGAGE = 0.15              # engage corrections only above this (hysteresis)
    DRIFT_RELEASE = 0.06             # release (disengage) when engaged and |drift| <= this
    DRIFT_FINE = 0.3                 # fine zone upper bound
    SPEED_FINE_DIV = 8.0             # fine zone speed = 1 - fd/SPEED_FINE_DIV (gentle)
    SPEED_FINE_CLAMP = 0.025         # fine zone max speed deviation from base
    REANCHOR_SETTLE = 2.5            # correction holdoff after init-sync/re-anchor (seconds)
    SPEED_CHANGE_MIN_INTERVAL = 1.0  # min seconds between actual mpv speed writes
    OFFSET_TRIM_MIN = 0.04           # auto-trim anchor when quiet median drift exceeds this
    OFFSET_SPREAD_MAX = 0.12         # max sample spread (max-min) for a stable window
    RATE_MIN_PPM = 100               # apply rate feed-forward only above this
    RATE_MAX_PPM = 5000              # sanity cap on believable rate error
    RATE_BIAS_CLAMP = 0.001          # max |rate bias| (fractional speed)
    LEARN_QUIET = 45.0               # corrections must be idle this long before learning
    LEARN_MIN_WINDOW = 60.0          # min clean sample-window span (seconds)
    TELEMETRY_INTERVAL = 60.0        # drift telemetry print interval
    PLAY_WARMUP_GRACE = 15.0         # suppress re-anchors during play-start decode warmup
    SONOS_STALL_SECS = 1.6           # Sonos reading older than this -> stall: hold video, skip corrections
    SONOS_STALL_RESTART_AFTER = 20.0 # stall this long -> force stream restart at last known position
    DRIFT_REANCHOR = 3.0             # |drift| > this -> hard re-anchor; below this the speed corrector catches up seamlessly
    REANCHOR_THROTTLE = 8.0          # min seconds between re-anchors
    SPEED_COARSE_DIV = 6.0           # coarse zone speed = base - fd/SPEED_COARSE_DIV
    SPEED_COARSE_CLAMP = 0.12        # coarse zone max speed deviation from base

    # --- Sonos volume worker ---
    VOLUME_DEBOUNCE_SEC = 0.15       # trailing debounce: drag burst -> one write of the newest value

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Sonos PC Streamer")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(720, 420)
        self.setMouseTracking(True)  # hover mouseMoveEvent: resize-cursor feedback
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self.player = None
        self._embedded = False
        self.current_uri = None
        self._resolved_audio_url = None
        self._duration_ms = 0
        self.seeking = False
        self._drag_pos = None
        self._dragging = False
        self._resize_edge = None
        self._resize_start_global = None
        self._resize_start_geom = None
        self._RESIZE_MARGIN = 10
        self._edge_cursor = None
        self._is_live = False
        self._resolved_audio_headers = {}
        self._hdr_enabled = load_settings().get("hdr_enabled", False)
        self._source_is_hdr = False
        self._nits = load_settings().get("nits", 1000)
        self.sync_ready = False
        self.initial_sync_done = False
        self.drift_f = 0.0
        self.last_resync = 0.0
        self.drift_hist = []
        self.corr_engaged = False
        self.last_speed_write = 0.0
        self.sync_rate_bias = 0.0
        self.last_poll_time = 0.0
        self.sync_offset_runtime = 0.0
        self.user_sync_offset = SYNC_OFFSET_SECONDS
        self._last_open_dir = load_settings().get("last_open_dir", "")
        self.fd_samples = []
        self.last_engage_time = 0.0
        self.last_telemetry = 0.0
        self.play_started_at = 0.0
        self.corrections_count = 0
        self.stall_active = False
        self.stall_started_at = 0.0
        self._pre_cross_size = None
        self._restoring_size = False
        self._aspect_fit_pending = False
        self._aspect_fit_pending_at = 0.0
        self.mpv_buffering = False
        self._screen_connected = False
        self._hdr_probe_done = False
        self._sync_logged = False
        self._sync_no_child_logged = False
        self.current_speed = 1.0
        self._seek_gen = 0
        self.seek_in_progress = False
        self._restart_attempts = 0
        self._force_fresh_resolve = False
        self._pending_start_pos = None
        self._soft_restarting = False
        self._resolved_video_candidates = []
        self.current_video_tier_index = None
        self._pending_tier_index = 0
        self.buffer_episodes = 0
        self.last_downgrade_at = 0.0
        self.av_lock = threading.Lock()
        self._playback_generation = 0
        self.shutdown_event = threading.Event()
        # Sonos volume serialization: slider events stage the newest value
        # here; the single volume worker (started below, next to the sync
        # monitor) is the only writer of speaker.volume.
        self.vol_lock = threading.Lock()
        self.pending_volume = None
        self.vol_event = threading.Event()
        self.user_volume_touched = False  # set on first user slider move
        self.correction_holdoff_until = 0.0
        self.last_soco_elapsed = 0.0
        self.last_soco_time = 0.0
        self._audio_tracks = []
        self._probe_data = None
        self._probe_timer: QTimer | None = None
        self._last_probed_uri = ""
        self._probe_error_reason = None
        self._selected_audio_index = -1
        self._subtitle_tracks = []
        self._sub_last_track_ids = ()

        central = QWidget()
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Custom title bar
        title_bar = QWidget()
        title_bar.setFixedHeight(36)
        title_bar.setObjectName("titleBar")
        title_bar_layout = QHBoxLayout(title_bar)
        title_bar_layout.setContentsMargins(12, 0, 4, 0)
        title_bar_layout.setSpacing(0)

        menu_btn = QPushButton("☰")
        menu_btn.setObjectName("menuBtn")
        menu_btn.setFixedSize(36, 28)
        menu_btn.clicked.connect(self._open_main_menu)
        title_bar_layout.insertWidget(0, menu_btn)
        self._menu_btn = menu_btn

        title_label = QLabel("Sonos PC Streamer")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setObjectName("titleCaption")
        title_bar_layout.addStretch(1)
        title_bar_layout.addWidget(title_label)
        title_bar_layout.addStretch(1)

        min_btn = QPushButton()
        min_btn.setObjectName("minBtn")
        min_btn.setFixedSize(36, 28)
        min_btn.clicked.connect(self.showMinimized)
        title_bar_layout.addWidget(min_btn)

        max_btn = QPushButton()
        max_btn.setObjectName("maxBtn")
        max_btn.setFixedSize(36, 28)
        max_btn.clicked.connect(self._toggle_maximized)
        title_bar_layout.addWidget(max_btn)

        close_btn = QPushButton()
        close_btn.setObjectName("closeBtn")
        close_btn.setFixedSize(36, 28)
        close_btn.clicked.connect(self.close)
        title_bar_layout.addWidget(close_btn)

        # Real Windows caption glyphs (Segoe Fluent Icons on Win11, MDL2 Assets on Win10).
        glyph_font = QFont()
        glyph_font.setFamilies(["Segoe Fluent Icons", "Segoe MDL2 Assets"])
        glyph_font.setPointSize(9)
        min_btn.setFont(glyph_font)
        max_btn.setFont(glyph_font)
        close_btn.setFont(glyph_font)
        min_btn.setText("\uE921")   # ChromeMinimize
        close_btn.setText("\uE8BB")  # ChromeClose
        max_btn.setText("\uE922")   # ChromeMaximize

        layout.addWidget(title_bar)

        # Video surface (mpv renders here via wid embedding)
        self._video_container = VideoFrame()
        layout.addWidget(self._video_container, stretch=1)

        content_area = QWidget()
        content_layout = QVBoxLayout(content_area)
        content_layout.setContentsMargins(12, 8, 12, 8)
        content_layout.setSpacing(6)

        # Input field + Browse live in the hamburger menu now (see below).
        self.uri_label = QLineEdit()
        self.uri_label.setPlaceholderText("File path or URL...")
        self.browse_btn = QPushButton("Browse")
        self.browse_btn.setObjectName("browseBtn")
        self.browse_btn.setFixedWidth(80)
        self.browse_btn.clicked.connect(self.browse_file)

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("lcdGreen")
        self.time_label.setMinimumWidth(100)

        self.play_btn = QPushButton(chr(0x25B6))  # play icon
        self.play_btn.setObjectName("playBtn")
        self.play_btn.setFixedWidth(50)
        self.play_btn.setFixedHeight(24)
        self.play_btn.setToolTip("Play")
        self.play_btn.clicked.connect(self.on_play)
        self.stop_btn = QPushButton(chr(0x25A0))  # stop icon
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setFixedWidth(50)
        self.stop_btn.setFixedHeight(24)
        self.stop_btn.setToolTip("Stop")
        self.stop_btn.clicked.connect(self.on_stop)
        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(0, 100)
        self.vol_slider.setValue(30)
        self.vol_slider.valueChanged.connect(self.on_volume_changed)

        seek_row = QHBoxLayout()
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, 1000)
        self.seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self.seek_slider.sliderMoved.connect(self._on_seek_moved)
        self.seek_slider.sliderReleased.connect(self._on_seek_released)
        seek_row.addWidget(self.play_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        seek_row.addWidget(self.stop_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        seek_row.addWidget(self.seek_slider, stretch=8)
        seek_row.addWidget(self.vol_slider, stretch=2)
        content_layout.addLayout(seek_row)

        self.sonos_label = QLabel("")
        self.sonos_label.setObjectName("lcdAmber")
        self.sonos_label.setMinimumWidth(120)
        self.speed_label = QLabel("")
        self.speed_label.setMinimumWidth(60)
        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setObjectName("statusLabel")
        bottom_row = QHBoxLayout()
        bottom_row.addWidget(self.time_label)
        bottom_row.addWidget(QLabel(chr(0x2014)))
        bottom_row.addWidget(self.sonos_label)
        bottom_row.addStretch()
        bottom_row.addWidget(self.speed_label)
        bottom_row.addWidget(self.status_label)
        content_layout.addLayout(bottom_row)

        # Fullscreen transport overlay (VLC-style; revealed by cursor at the
        # bottom). Native child of the main window so it always stacks above
        # mpv's embedded child window. Opaque paint only.
        self._fs_controls = QWidget(self)
        self._fs_controls.setObjectName("fsControls")
        self._fs_controls.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self._fs_controls.setCursor(Qt.CursorShape.ArrowCursor)
        self._fs_controls.hide()
        fs_row = QHBoxLayout(self._fs_controls)
        fs_row.setContentsMargins(10, 6, 10, 6)
        fs_row.setSpacing(8)
        self._fs_play_btn = QPushButton(chr(0x25B6))
        self._fs_play_btn.setObjectName("playBtn")
        self._fs_play_btn.setFixedWidth(50)
        self._fs_play_btn.setFixedHeight(24)
        self._fs_play_btn.setToolTip("Play/Pause")
        self._fs_play_btn.clicked.connect(self._toggle_pause)
        self._fs_stop_btn = QPushButton(chr(0x25A0))
        self._fs_stop_btn.setObjectName("stopBtn")
        self._fs_stop_btn.setFixedWidth(50)
        self._fs_stop_btn.setFixedHeight(24)
        self._fs_stop_btn.setToolTip("Stop")
        self._fs_stop_btn.clicked.connect(self._fs_stop)
        self._fs_time_label = QLabel("00:00 / 00:00")
        self._fs_time_label.setObjectName("lcdGreen")
        self._fs_time_label.setMinimumWidth(100)
        self.fs_vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.fs_vol_slider.setRange(0, 100)
        self.fs_vol_slider.setValue(30)
        self.fs_vol_slider.valueChanged.connect(self.on_volume_changed)
        self._fs_seek_slider = QSlider(Qt.Orientation.Horizontal)
        self._fs_seek_slider.setRange(0, 1000)
        self._fs_seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self._fs_seek_slider.sliderMoved.connect(self._on_seek_moved)
        self._fs_seek_slider.sliderReleased.connect(lambda: self._on_seek_released(self._fs_seek_slider))
        fs_row.addWidget(self._fs_play_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        fs_row.addWidget(self._fs_stop_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        fs_row.addWidget(self._fs_seek_slider, stretch=8)
        fs_row.addWidget(self._fs_time_label)
        fs_row.addWidget(self.fs_vol_slider, stretch=2)
        self._fs_controls_timer = QTimer(self)
        self._fs_controls_timer.timeout.connect(self._fs_controls_tick)

        # Feature widgets relocated into the hamburger menu (logic unchanged).
        self.fs_btn = QPushButton("Fullscreen")
        self.fs_btn.setObjectName("browseBtn")
        self.fs_btn.clicked.connect(self.on_fullscreen)
        self.hdr_btn = QPushButton("HDR: ON" if self._hdr_enabled else "HDR: OFF")
        self.hdr_btn.setCheckable(True)
        self.hdr_btn.setChecked(self._hdr_enabled)
        self.hdr_btn.setObjectName("hdrBtn")
        self.hdr_btn.toggled.connect(self._toggle_hdr)
        self.nits_label = QLabel("Nits")
        self.nits_spin = QSpinBox()
        self.nits_spin.setRange(200, 10000)
        self.nits_spin.setSingleStep(50)
        self.nits_spin.setValue(self._nits)
        self.nits_spin.setFixedWidth(80)
        self.nits_spin.setEnabled(self._hdr_enabled)
        self.nits_spin.valueChanged.connect(self._on_nits_changed)
        self.audio_track_label = QLabel("Audio Track:")
        self.audio_track_label.setFixedWidth(86)
        self.audio_track_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.audio_track_combo = QComboBox()
        self.audio_track_combo.setMinimumWidth(260)
        self.audio_track_combo.addItem("Default (auto)", -1)
        self.audio_track_combo.setEnabled(False)
        self.audio_track_combo.currentIndexChanged.connect(self._on_audio_track_changed)
        self.sub_label = QLabel("Subtitles:")
        self.sub_label.setFixedWidth(86)
        self.sub_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.sub_combo = QComboBox()
        self.sub_combo.setMinimumWidth(260)
        self.sub_combo.addItem("No subtitles", -1)
        self.sub_combo.setEnabled(False)
        self.sub_combo.currentIndexChanged.connect(self._on_sub_changed)
        self.sub_btn = QPushButton("Subs: ON")
        self.sub_btn.setCheckable(True)
        self.sub_btn.setChecked(True)
        self.sub_btn.setObjectName("subBtn")
        self.sub_btn.toggled.connect(self._on_sub_toggle)
        self.vol_label = QLabel("Volume: 30")
        self.sync_offset_label = QLabel(f"Video Sync: {self.user_sync_offset:+.2f}s")
        self.sync_offset_label.setMinimumWidth(112)
        self.sync_minus_btn = QPushButton("−")
        self.sync_minus_btn.setObjectName("browseBtn")
        self.sync_minus_btn.setFixedSize(36, 28)
        self.sync_minus_btn.setStyleSheet("padding: 2px 0px; font-size: 14px;")
        self.sync_minus_btn.clicked.connect(self._on_sync_nudge_minus)
        self.sync_plus_btn = QPushButton("+")
        self.sync_plus_btn.setObjectName("browseBtn")
        self.sync_plus_btn.setFixedSize(36, 28)
        self.sync_plus_btn.setStyleSheet("padding: 2px 0px; font-size: 14px;")
        self.sync_plus_btn.clicked.connect(self._on_sync_nudge_plus)

        self._status_signal.connect(self.status_label.setText)
        self._schedule_color_log.connect(self._on_schedule_color_log)
        self.sonos_pos_signal.connect(self._on_sonos_pos)
        self._probe_signal.connect(self._on_probe_result)
        self._playback_end_signal.connect(self._on_playback_end)
        self._video_reconfig_signal.connect(self._on_video_reconfig)
        self._fs_menu_signal.connect(self._show_fs_menu)
        self._sonos_vol_signal.connect(self.on_sonos_volume)
        self.live_state_signal.connect(self.on_live_state)
        self.downgrade_signal.connect(self.downgrade_video_tier)
        self.uri_label.textChanged.connect(self._on_uri_changed)

        self._sonos_timer = QTimer(self)
        self._sonos_timer.timeout.connect(self._poll_sonos_position)
        self._sonos_timer.start(250)

        # Fetch the initial Sonos volume off the GUI thread (never block the
        # event loop on Sonos HTTP); the result arrives via _sonos_vol_signal.
        threading.Thread(
            target=self._initial_volume_reader, daemon=True, name="sonos-initial-volume"
        ).start()

        layout.addWidget(content_area)
        self.title_bar = title_bar
        self.content_area = content_area
        self.min_btn = min_btn
        self.max_btn = max_btn
        self.close_btn = close_btn

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_position)
        self._poll_timer.start(500)

        self._sync_thread = threading.Thread(
            target=self.sync_monitor_loop, daemon=True, name="sonos-sync-monitor"
        )
        self._sync_thread.start()
        self._vol_thread = threading.Thread(
            target=self.volume_worker, daemon=True, name="sonos-volume"
        )
        self._vol_thread.start()
        self._video_container.set_sync_callback(self._sync_mpv_child_window)
        self._video_container.set_ui_callback(self._container_ui_event)

        # Hamburger menu — hosts the relocated feature widgets (logic unchanged).
        def _menu_row(*widgets):
            holder = QWidget()
            holder.setStyleSheet("background: transparent;")
            row = QHBoxLayout(holder)
            row.setContentsMargins(6, 2, 6, 2)
            row.setSpacing(6)
            for widget in widgets:
                row.addWidget(widget)
            row.addStretch()
            return holder

        def _widget_action(menu, widget):
            action = QWidgetAction(menu)
            action.setDefaultWidget(widget)
            menu.addAction(action)
            return action

        self._main_menu = QMenu(self)
        _widget_action(self._main_menu, _menu_row(self.uri_label, self.browse_btn))
        _widget_action(self._main_menu, self.fs_btn)
        _widget_action(self._main_menu, self.hdr_btn)
        _widget_action(self._main_menu, _menu_row(self.nits_label, self.nits_spin))
        self._main_menu.addSeparator()
        audio_menu = self._main_menu.addMenu("Audio Track")
        _widget_action(audio_menu, self.audio_track_combo)
        sub_menu = self._main_menu.addMenu("Subtitles")
        _widget_action(sub_menu, _menu_row(self.sub_combo, self.sub_btn))
        self._main_menu.addSeparator()
        _widget_action(self._main_menu,
                       _menu_row(self.sync_minus_btn, self.sync_offset_label, self.sync_plus_btn))

        if sys.platform == "win32":
            try:
                dwm = ctypes.windll.dwmapi
                dark = ctypes.c_int(1)  # DWMWA_USE_IMMERSIVE_DARK_MODE -> TRUE
                dwm.DwmSetWindowAttribute(int(self.winId()), 20, ctypes.byref(dark), 4)  # type: ignore[attr-defined]
                preference = ctypes.c_uint(0)  # DWMWCP_DONOTROUND
                dwm.DwmSetWindowAttribute(  # type: ignore[attr-defined]
                    int(self.winId()), _DWMWA_WINDOW_CORNER_PREFERENCE,
                    ctypes.byref(preference), ctypes.sizeof(preference),
                )
                none_border = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE — borderless
                dwm.DwmSetWindowAttribute(int(self.winId()), 34, ctypes.byref(none_border), 4)  # type: ignore[attr-defined]
            except OSError:
                pass

        self.installEventFilter(self)
        self.setMouseTracking(True)
        for qwidget in self.findChildren(QWidget):
            if qwidget.window() is not self:
                continue  # popup windows (menus) and their embedded widgets
            qwidget.installEventFilter(self)
            qwidget.setMouseTracking(True)

    def _on_schedule_color_log(self):
        # Poll until video is actually loaded before logging color state
        if not self.player:
            return
        try:
            vo = self.player.current_vo
            if vo is not None:
                self._log_color_info()
                self._check_hdr_output()
                QTimer.singleShot(6000, self._log_vo_diag)
                return
        except (Exception,) as e:
            _print(f"[MPV] VO check failed: {e}")
        QTimer.singleShot(1000, self._on_schedule_color_log)

    def _poll_sonos_position(self):
        # QTimer tick: speed indicator only. Sync correction lives in sync_monitor_loop.
        spd = self.current_speed
        if abs(spd - 1.0) > 0.005:
            self.speed_label.setText(f"{spd:.3f}x")
        else:
            self.speed_label.setText("")

    def sync_monitor_loop(self):
        sync_controller.run_monitor_loop(self)

    def restart_sonos_at_current_pos(self):
        sync_controller.restart_sonos_at_current_pos(self)

    def apply_drift_correction(self, drift, audio_pos):
        sync_controller.apply_drift_correction(self, drift, audio_pos)

    def learn_from_drift(self, now):
        sync_controller.learn_from_drift(self, now)

    def _on_sonos_pos(self, pos_str, audio_pos):
        if audio_pos > 0:
            self.sonos_label.setText(f"Sonos: {fmt_time(int(audio_pos*1000))}")
        else:
            self.sonos_label.setText(f"Sonos: {pos_str}")

    def _on_playback_end(self, reason):
        if not self.player or not self.current_uri:
            return
        if reason not in ("eof", "error"):
            _print(f"[PLAY] end-file reason={reason!r} — ignoring")
            return
        pos = 0.0
        try:
            pos = float(self.player.time_pos or 0.0)
        except (Exception,) as e:
            _print(f"[PLAY] time_pos read failed: {e}")
        dur = 0.0
        try:
            dur = float(self.player.duration or 0.0)
        except (Exception,) as e:
            _print(f"[PLAY] duration read failed: {e}")
        if not dur and self._probe_data and self._probe_data.get("duration"):
            try:
                dur = float(self._probe_data.get("duration") or 0.0)
            except (Exception,) as e:
                _print(f"[PLAY] Probe duration read failed: {e}")
        natural = self._is_live or (reason == "eof" and (dur <= 0 or pos >= dur - 5.0))
        if natural:
            _print("[PLAY] Playback reached end — stopping Sonos stream")
            self.do_stop()
            return
        if getattr(self, "_restart_attempts", 0) >= 2:
            _print(f"[PLAY] Stream ended early (reason={reason}, pos={pos:.1f}s) — retries exhausted")
            self._status_signal.emit("Stream ended early — stopped")
            self.do_stop()
            return
        self._restart_attempts += 1
        _print(f"[PLAY] Stream ended early (reason={reason}, pos={pos:.1f}s) — auto-restart {self._restart_attempts}/2")
        self._status_signal.emit("Reconnecting...")
        generation = self._playback_generation
        QTimer.singleShot(1500, lambda: self._soft_restart(max(pos, 0.0), generation))

    def _soft_restart(self, pos_sec, generation):
        if self.shutdown_event.is_set() or not self._playback_is_current(generation):
            return  # shutdown requested or a newer play/stop superseded this restart
        if not self.current_uri:
            return  # user stopped during the settle delay
        uri = self.current_uri
        _print(f"[PLAY] Soft restart: fresh resolve at {pos_sec:.1f}s")
        self._force_fresh_resolve = True
        self._pending_start_pos = pos_sec
        self._soft_restarting = True
        try:
            self.do_play_uri(uri)
        finally:
            self._soft_restarting = False

    def downgrade_video_tier(self):
        """Restart playback at the next-lower video tier (recurring-underrun
        fallback). One tier per call; a no-op when no lower tier exists or a
        downgrade/restart is already in flight."""
        if self.shutdown_event.is_set() or self._soft_restarting:
            return
        if self._pending_tier_index:
            return  # a downgrade is already pending/in flight
        cands = self._resolved_video_candidates
        cur = self.current_video_tier_index
        if cur is None or cur + 1 >= len(cands):
            return  # no lower tier
        pos = sync_controller.get_seek_base_pos() + self.last_soco_elapsed + min(
            time.time() - self.last_soco_time, self.EXTRAP_CAP)
        if pos < 0:
            pos = 0.0
        _print(f"[PLAY] Recurring video underruns — downgrading to tier index {cur + 1} ({cands[cur + 1].tier}p)")
        self._pending_tier_index = cur + 1
        self.buffer_episodes = 0  # fresh count for the new tier
        self._force_fresh_resolve = True
        self._pending_start_pos = pos
        self._soft_restarting = True
        try:
            self.do_play_uri(self.current_uri)
        finally:
            self._soft_restarting = False

    def reset_sync_state(self):
        sync_controller.reset_sync_state(self)

    def _on_uri_changed(self, text):
        if not text.strip():
            return
        timer = self._probe_timer
        if timer:
            timer.stop()
        else:
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._schedule_probe(self.uri_label.text().strip()))  # type: ignore[attr-defined]
            self._probe_timer = timer
        timer.start(800)

    def _schedule_probe(self, uri):
        if not uri or uri == self._last_probed_uri:
            return
        self._last_probed_uri = uri
        self.audio_track_combo.blockSignals(True)
        self.audio_track_combo.clear()
        self.audio_track_combo.addItem("Probing...", -2)
        self.audio_track_combo.setEnabled(False)
        self.audio_track_combo.blockSignals(False)
        self._audio_tracks = []
        self._probe_data = None
        self._selected_audio_index = -1
        threading.Thread(target=self._probe_worker, args=(uri,), daemon=True).start()

    def _probe_worker(self, uri):
        result = probe_audio_tracks(uri)
        self._probe_signal.emit(result)

    def _on_probe_result(self, result):
        self.audio_track_combo.blockSignals(True)
        self.audio_track_combo.clear()
        if result is None:
            code = media.get_last_yt_error()
            self._probe_error_reason = code
            if code:
                self.audio_track_combo.addItem(yt_error_text(code), -1)
                self.status_label.setText("Can't play: " + yt_error_text(code))
            else:
                self.audio_track_combo.addItem("Probe failed", -1)
                self.status_label.setText("Probe failed")
            self.audio_track_combo.setEnabled(False)
            self._audio_tracks = []
            self._probe_data = None
        else:
            tracks = result.get("audio_tracks", [])
            self._probe_data = result
            self._probe_error_reason = None
            self._audio_tracks = tracks
            if tracks:
                for i, t in enumerate(tracks):
                    display = t.get("title", f"Stream {t['index']}")
                    self.audio_track_combo.addItem(display, i)
                self.audio_track_combo.setEnabled(True)
            else:
                self.audio_track_combo.addItem("No audio tracks found", -1)
                self.audio_track_combo.setEnabled(False)
        self.audio_track_combo.setCurrentIndex(0)
        self.audio_track_combo.blockSignals(False)

    def _on_audio_track_changed(self, index):
        idx = self.audio_track_combo.itemData(index)
        self._selected_audio_index = idx if idx is not None else -1
        if self._probe_data and self._probe_data.get("type") == "local":
            stream_server.set_selected_audio_track(self._selected_audio_index)
        else:
            stream_server.set_selected_audio_track(-1)
        _print(f"[AUDIO] Track selected: index={self._selected_audio_index}, global={stream_server.get_selected_audio_track()}")
        # Apply immediately when a local file is actively playing:
        # re-spawn the audio stream at the current position with the new track.
        if (self._probe_data and self._probe_data.get("type") == "local"
                and self.current_uri and self.player
                and not self.seek_in_progress
                and self._duration_ms > 0):
            pos_ms, _ = self.get_position()
            if pos_ms > 0:
                _print(f"[AUDIO] Applying track change at {float(pos_ms) / 1000.0:.1f}s")
                self._do_seek(pos_ms)

    def _check_subtitle_tracks(self):
        """Poll mpv track_list and update subtitle combo if tracks changed."""
        if not self.player:
            return
        try:
            track_list = self.player.track_list
        except (Exception,):
            return
        if not track_list:
            return
        subs = [t for t in track_list if t.get("type") == "sub"]
        new_ids = tuple(t.get("id", 0) for t in subs)
        if new_ids == self._sub_last_track_ids:
            self._sync_sub_selection()
            return
        self._sub_last_track_ids = new_ids
        self._subtitle_tracks = subs
        self.sub_combo.blockSignals(True)
        self.sub_combo.clear()
        if not subs:
            self.sub_combo.addItem("No subtitles", -1)
            self.sub_combo.setEnabled(False)
        else:
            self.sub_combo.addItem("Off", 0)
            for t in subs:
                lang = (t.get("lang") or t.get("title") or "").upper() or f"Track {t['id']}"
                codec = t.get("codec_name", t.get("codec", ""))
                label = f"{lang} ({codec})" if codec else lang
                self.sub_combo.addItem(label, t["id"])
            self.sub_combo.setEnabled(True)
        self._sync_sub_selection()
        self.sub_combo.blockSignals(False)

    def _sync_sub_selection(self):
        """Sync combo selection and toggle button to current mpv subtitle state."""
        if not self.player:
            return
        try:
            current_sid = self.player.sid or 0
        except (Exception,):
            current_sid = 0
        idx = self.sub_combo.findData(current_sid)
        if idx >= 0 and idx != self.sub_combo.currentIndex():
            self.sub_combo.blockSignals(True)
            self.sub_combo.setCurrentIndex(idx)
            self.sub_combo.blockSignals(False)
        try:
            vis = self.player.sub_visibility
            if vis != self.sub_btn.isChecked():
                self.sub_btn.blockSignals(True)
                self.sub_btn.setChecked(vis)
                self.sub_btn.blockSignals(False)
        except (Exception,) as e:
            _print(f"[SUB] Visibility read failed: {e}")

    def _on_sub_changed(self, index):
        tid = self.sub_combo.itemData(index)
        if tid is None or not self.player:
            return
        try:
            self.player.sid = tid
            _print(f"[SUB] Track set to sid={tid}")
        except (Exception,) as e:
            _print(f"[SUB] Error setting sid: {e}")

    def _on_sub_toggle(self, checked):
        if not self.player:
            return
        try:
            self.player.sub_visibility = checked
            self.sub_btn.setText("Subs: ON" if checked else "Subs: OFF")
            _print(f"[SUB] Visibility: {'ON' if checked else 'OFF'}")
        except (Exception,) as e:
            _print(f"[SUB] Error toggling visibility: {e}")

    def _color_output_kwargs(self, force_sdr=False):
        """Return mpv options for the current color/output configuration.

        The HDR toggle describes the DISPLAY, not the source. The detected
        source only decides how gamut/tone mapping is handled.
        When force_sdr is set, always return the plain SDR transform (embedded fallback).
        """
        if not self._hdr_enabled or force_sdr:
            return {
                "target_prim": "auto",
                "target_trc": "auto",
                "target_peak": "auto",
                "gamut_mapping_mode": "auto",
                "target_colorspace_hint": "no",
                "inverse_tone_mapping": "no",
                "hdr_compute_peak": "auto",
            }
        kwargs = {
            "target_prim": "bt.2020",
            "target_trc": "pq",
            "target_peak": self._nits,
            "target_colorspace_hint": "yes",
            "target_colorspace_hint_mode": "target",
            "inverse_tone_mapping": "no",
            "hdr_compute_peak": "auto",
        }
        if self._source_is_hdr:
            kwargs["gamut_mapping_mode"] = "clip"
        else:
            kwargs["gamut_mapping_mode"] = "auto"
            kwargs["inverse_tone_mapping"] = "yes"
            kwargs["hdr_compute_peak"] = "no"
        return kwargs

    def _configure_color_output(self, force_sdr=False):
        """Apply the current color/output configuration to the player."""
        if not self.player:
            return
        try:
            for key, value in self._color_output_kwargs(force_sdr).items():
                setattr(self.player, key, value)
            if self._hdr_enabled and not force_sdr:
                src = "HDR" if self._source_is_hdr else "SDR (upconverted)"
                _print(f"[COLOR] Target = HDR, {self._nits} nits (src={src})")
            else:
                _print("[COLOR] Target = SDR" + (" (embedded fallback)" if force_sdr else ""))
        except (Exception,) as e:
            _print(f"[HDR] Apply error: {e}")

    def _check_hdr_output(self, attempts=0):
        """Embedded mode: verify mpv actually outputs HDR; fall back to the
        plain SDR transform when the child-window swapchain isn't HDR."""
        if not (self._embedded and self._hdr_enabled) or self._hdr_probe_done or not self.player:
            return
        if getattr(self, "_aspect_fit_pending", False):
            if time.time() - getattr(self, "_aspect_fit_pending_at", 0.0) < 5.0:
                QTimer.singleShot(1000, lambda: self._check_hdr_output(attempts))
                return
            self._aspect_fit_pending = False
        try:
            if playback_controller.get_mpv_property(self.player,"video-params/w") is None:
                if attempts < 20:
                    QTimer.singleShot(1000, lambda: self._check_hdr_output(attempts + 1))
                return
        except (Exception,):
            return
        try:
            gamma = playback_controller.get_mpv_property(self.player,"video-output-params/gamma")
        except (Exception,):
            gamma = None
        self._hdr_probe_done = True
        if gamma != "pq":
            # noinspection PyStringConversionWithoutDunderMethod
            gamma_repr = f"{gamma}" if gamma is not None else "None"
            _print(f"[COLOR] Embedded output is not HDR (gamma={gamma_repr}) — falling back to SDR transform")
            self._configure_color_output(force_sdr=True)
            self._schedule_color_log.emit()

    def _wait_for_video_ready(self, uri, timeout=10.0):
        """Hold the Sonos stream start until mpv's video pipeline is actually
        live (slow 4K/VP9 init otherwise leaves the audio running seconds
        ahead while no video frames exist). Audio-only sources skip the wait.
        Runs on the resolve thread. Returns True when playback went stale, the
        source turned out audio-only, or video decoded in time; False only
        when the timeout expired without a decoded video track."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.player or self.current_uri != uri:
                return True
            try:
                tracks = playback_controller.get_mpv_property(self.player,"track-list")
                if isinstance(tracks, (list, tuple)) and tracks:
                    has_video = any(
                        isinstance(t, dict) and t.get("type") == "video"
                        for t in tracks
                    )
                else:
                    # Empty list = tracks not parsed yet; unreadable = unknown.
                    # Both keep waiting (bounded by the timeout).
                    has_video = True
            except (Exception,):
                has_video = True
            if not has_video:
                return True  # audio-only content: nothing to wait for
            try:
                if playback_controller.get_mpv_property(self.player,"vo-configured") \
                        and playback_controller.get_mpv_property(self.player,"video-params/w"):
                    return True
            except (Exception,) as e:
                _print(f"[PLAY] Video-decode probe failed: {e}")
            time.sleep(0.2)
        return False

    def _check_video_startup_health(self, tier, generation):
        """Sample the just-started stream for ~3.0s and return the
        (healthy, reason) verdict from video_health_verdict.

        Counts paused-for-cache stalls, measures the frame-drop delta across
        the window, and reads the active hardware decoder; (False, "stale")
        when the playback generation is superseded. Runs on the resolve
        thread."""
        if not self._playback_is_current(generation):
            return False, "stale"
        try:
            drop_start = int(playback_controller.get_mpv_property(self.player,"frame-drop-count"))
        except (Exception,):
            drop_start = None
        readable_count = 0
        true_count = 0
        for _ in range(6):
            time.sleep(0.5)
            try:
                paused = playback_controller.get_mpv_property(self.player,"paused-for-cache")
            except (Exception,):
                paused = None
            if paused is not None:
                readable_count += 1
                if paused:
                    true_count += 1
        try:
            drop_end = int(playback_controller.get_mpv_property(self.player,"frame-drop-count"))
        except (Exception,):
            drop_end = None
        if drop_start is not None and drop_end is not None:
            drop_delta = max(0, drop_end - drop_start)
        else:
            drop_delta = 0
        try:
            hwdec = playback_controller.get_mpv_property(self.player,"hwdec-current")
        except (Exception,):
            hwdec = None
        cache_ratio = true_count / readable_count if readable_count else 0.0
        if not self._playback_is_current(generation):
            return False, "stale"
        return video_health_verdict(hwdec, tier, drop_delta, cache_ratio)

    def _on_video_reconfig(self):
        """mpv applied new video dimensions — resize the window so the video
        fills its area at the correct aspect (VLC-style, cover-grow)."""
        if not self._embedded or not self.player or self.isFullScreen():
            return
        if self._resize_edge or self._dragging or self._restoring_size:
            return  # user is actively moving/resizing — never fight them
        QTimer.singleShot(300, self._aspect_fit_read)

    def _aspect_fit_read(self, attempts=0):
        if not self._embedded or not self.player:
            return
        try:
            vw = int(playback_controller.get_mpv_property(self.player,"video-params/w"))
            vh = int(playback_controller.get_mpv_property(self.player,"video-params/h"))
            vo_cfg = playback_controller.get_mpv_property(self.player,"vo-configured")
        except (Exception,):
            vw = vh = 0
            vo_cfg = None
        if not vw or not vh or not vo_cfg:
            if attempts < 4:
                QTimer.singleShot(400, lambda: self._aspect_fit_read(attempts + 1))
            else:
                self._aspect_fit_pending = False
            return
        try:
            rot = playback_controller.get_mpv_property(self.player,"video-params/rotate") or 0
        except (Exception,):
            rot = 0
        if rot in (90, 270):
            vw, vh = vh, vw
        self._aspect_fit_pending = False
        self._apply_video_aspect_fit(vw, vh)

    def _log_vo_diag(self):
        """One-shot presentation snapshot for diagnosing blank/white video."""
        if not self.player:
            return
        names = ("vo-configured", "osd-width", "osd-height", "video-params/w",
                 "video-params/h", "video-format", "video-codec", "hwdec-current",
                 "frame-drop-count")
        parts = []
        for name in names:
            try:
                val = playback_controller.get_mpv_property(self.player,name)
            except (Exception,):
                val = None
            # noinspection PyStringConversionWithoutDunderMethod
            val_text = f"{val}" if val is not None else "n/a"
            parts.append(f"{name}={val_text}")
        _print(f"[MPV] diag: {' '.join(parts)}")

    def _apply_video_aspect_fit(self, vw, vh):
        """Cover-grow fit: keep the axis that already fits, grow the other so
        the video exactly fills its area (no black bars), capped to the
        screen's available work area."""
        if vw <= 0 or vh <= 0 or self.isFullScreen() or self.isMaximized():
            return
        cont = self._video_container.size()
        if cont.width() < 50 or cont.height() < 50:
            return
        vid_aspect = vw / vh
        cur_aspect = cont.width() / cont.height()
        if abs(cur_aspect - vid_aspect) <= 0.01 * vid_aspect:
            return  # close enough — no visible bars, avoid resize jitter
        win = self.size()
        chrome_w = max(win.width() - cont.width(), 0)
        chrome_h = max(win.height() - cont.height(), 0)
        scale = max(cont.width() / vw, cont.height() / vh)
        screen = self.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            scale = min(scale, max((avail.width() - chrome_w) / vw, 0.1),
                        max((avail.height() - chrome_h) / vh, 0.1))
        target_w = round(vw * scale) + chrome_w
        target_h = round(vh * scale) + chrome_h
        min_hint = self.minimumSizeHint()
        target_w = max(target_w, min_hint.width())
        target_h = max(target_h, min_hint.height())
        if (target_w, target_h) == (win.width(), win.height()):
            return
        _print(f"[PLAY] Aspect fit: video {vw}x{vh} → window {target_w}x{target_h}")
        self.resize(target_w, target_h)
        self._pre_cross_size = QSize(target_w, target_h)
        if screen is not None:
            avail = screen.availableGeometry()
            geom = self.frameGeometry()
            dx = 0
            dy = 0
            if geom.right() > avail.right():
                dx = avail.right() - geom.right()
            if geom.left() + dx < avail.left():
                dx = avail.left() - geom.left()
            if geom.bottom() > avail.bottom():
                dy = avail.bottom() - geom.bottom()
            if geom.top() + dy < avail.top():
                dy = avail.top() - geom.top()
            if dx or dy:
                self.move(self.x() + dx, self.y() + dy)

    def _sync_mpv_child_window(self):
        """Keep mpv's embedded child window exactly covering the container.

        Sizes come from the parent's client rect in physical pixels
        (GetClientRect — exact, no logical-pixel math, no rounding drift
        across DPI changes) and the update is skipped entirely when the
        child already matches (prevents cumulative growth loops).
        """
        if not (getattr(self, "_embedded", False) and self.player):
            return
        try:
            container = self._video_container
            user32 = ctypes.windll.user32
            parent = wintypes.HWND(int(container.winId()))
            prect = wintypes.RECT()
            if not user32.GetClientRect(parent, ctypes.byref(prect)):  # type: ignore[attr-defined]
                return
            w = prect.right - prect.left
            h = prect.bottom - prect.top
            if w <= 0 or h <= 0:
                return
            children = []
            EnumChildProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            def _on_child(hwnd, _lparam):
                children.append(hwnd)
                return True

            cb = EnumChildProc(_on_child)  # type: ignore[arg-type]
            user32.EnumChildWindows(parent, cb, None)  # type: ignore[attr-defined]
            if not children:
                if not getattr(self, "_sync_no_child_logged", False):
                    self._sync_no_child_logged = True
                    _print(f"[MPV] sync: no child windows under container yet (parent client {w}x{h})")
                return
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            for child in children:
                crect = wintypes.RECT()
                if user32.GetClientRect(child, ctypes.byref(crect)):  # type: ignore[attr-defined]
                    cw = crect.right - crect.left
                    ch = crect.bottom - crect.top
                    if cw == w and ch == h:
                        continue  # already exact — avoid churn
                user32.SetWindowPos(child, None, 0, 0, w, h, SWP_NOZORDER | SWP_NOACTIVATE)  # type: ignore[attr-defined]
            if not getattr(self, "_sync_logged", False):
                self._sync_logged = True
                rects = []
                for child in children:
                    r = wintypes.RECT()
                    if user32.GetWindowRect(child, ctypes.byref(r)):  # type: ignore[attr-defined]
                        rects.append((r.right - r.left, r.bottom - r.top))
                rects_text = ", ".join(f"{a}x{b}" for a, b in rects)
                _print(f"[MPV] sync: {len(children)} child window(s) resized to {w}x{h}; screen rects {rects_text}")
        except (Exception,) as e:
            _print(f"[MPV] Resize sync failed: {e}")

    def showEvent(self, event):
        window_handle = self.windowHandle()
        if window_handle is not None and not self._screen_connected:
            window_handle.screenChanged.connect(self._on_screen_changed)
            self._screen_connected = True
        super().showEvent(event)

    def _on_screen_changed(self, _screen):
        QTimer.singleShot(0, self._restore_pre_cross_size)

    def _restore_pre_cross_size(self):
        if self.isFullScreen() or self.isMaximized() or self._restoring_size or self._pre_cross_size is None:
            return
        target = self._pre_cross_size
        if (self.width(), self.height()) != (target.width(), target.height()):
            self._restoring_size = True
            try:
                self.resize(target.width(), target.height())
            finally:
                QTimer.singleShot(50, self._restore_done)

    def _restore_done(self):
        self._restoring_size = False

    def moveEvent(self, event):
        if not (self._dragging or self._resize_edge or self._restoring_size) and not self.isFullScreen() and not self.isMaximized():
            self._pre_cross_size = self.size()
        super().moveEvent(event)

    def resizeEvent(self, event):
        if self._resize_edge and not self._restoring_size:
            self._pre_cross_size = event.size()
        super().resizeEvent(event)
        if self.isFullScreen():
            self._position_fs_controls()

    def _container_ui_event(self, kind, event):
        """Mouse events over the native video surface never reach the main
        window — edge-cursor feedback and edge-resize are handled here. Note:
        "leave" arrives as a plain QEvent without a position, so the global
        position is only extracted for "move"/"press" mouse events."""
        if self.isFullScreen():
            return
        if kind == "move":
            gpos = event.globalPosition().toPoint()
            if self._resize_edge:
                self._apply_resize_drag(gpos)
            else:
                self._update_resize_cursor(gpos)
        elif kind == "press":
            if event.button() == Qt.MouseButton.LeftButton:
                gpos = event.globalPosition().toPoint()
                edge = self._edge_hit(self.mapFromGlobal(gpos))
                if edge:
                    self._resize_edge = edge
                    self._resize_start_global = gpos
                    self._resize_start_geom = self.frameGeometry()
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif kind == "release":
            self._finish_resize()
        elif kind == "leave":
            if not self._resize_edge:
                self.setCursor(Qt.CursorShape.ArrowCursor)

    def _update_resize_cursor(self, global_pos):
        edge = self._edge_hit(self.mapFromGlobal(global_pos))
        if edge & Qt.Edge.LeftEdge or edge & Qt.Edge.RightEdge:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif edge & Qt.Edge.TopEdge or edge & Qt.Edge.BottomEdge:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def _apply_resize_drag(self, global_pos):
        start = self._resize_start_geom
        mdx = global_pos.x() - self._resize_start_global.x()
        mdy = global_pos.y() - self._resize_start_global.y()
        new_x, new_y = start.x(), start.y()
        new_w, new_h = start.width(), start.height()
        edge = self._resize_edge
        if edge & Qt.Edge.LeftEdge:
            new_w = start.width() - mdx
            if new_w >= self.minimumWidth():
                new_x = start.x() + mdx
        if edge & Qt.Edge.TopEdge:
            new_h = start.height() - mdy
            if new_h >= self.minimumHeight():
                new_y = start.y() + mdy
        if edge & Qt.Edge.RightEdge:
            new_w = max(self.minimumWidth(), start.width() + mdx)
        if edge & Qt.Edge.BottomEdge:
            new_h = max(self.minimumHeight(), start.height() + mdy)
        self.setGeometry(QRect(new_x, new_y, new_w, new_h))

    def _finish_resize(self):
        self._restore_edge_cursor()
        if self.mouseGrabber() is self:
            self.releaseMouse()
        self._resize_edge = None
        self._resize_start_global = None
        self._resize_start_geom = None
        self._pre_cross_size = self.size()

    def eventFilter(self, obj, event):
        """Manual edge resizing for the frameless window. Child widgets
        swallow their own mouse events, so the filter (installed on the
        window and every child) mirrors the hover cursor via an application
        cursor override and drives the resize drag from here."""
        if obj is self and event.type() == QEvent.Type.WindowStateChange:
            self._sync_max_glyph()
        if self.isFullScreen() or self.isMaximized() or not isinstance(event, QMouseEvent):
            return False
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            # Interactive widgets keep their clicks even inside the edge band
            # (e.g. the close button sits a few px from the right border).
            if isinstance(obj, (QAbstractButton, QSlider, QComboBox, QAbstractSpinBox)):
                return False
            edge = self._edge_hit(self.mapFromGlobal(event.globalPosition().toPoint()))
            if not edge:
                return False
            self._resize_edge = edge
            self._resize_start_global = event.globalPosition().toPoint()
            self._resize_start_geom = self.frameGeometry()
            self._restore_edge_cursor()
            self.grabMouse(self._resize_cursor_shape(self.mapFromGlobal(self._resize_start_global)))
            return True
        if event.type() == QEvent.Type.MouseMove:
            gpos = event.globalPosition().toPoint()
            if self._resize_edge:
                self._apply_resize_drag(gpos)
                return True
            self._update_edge_cursor(self._resize_cursor_shape(self.mapFromGlobal(gpos)))
            return False
        if event.type() == QEvent.Type.MouseButtonRelease and self._resize_edge:
            self._finish_resize()
            return True
        return False

    def _resize_cursor_shape(self, pos):
        """Cursor for a window-local position: diagonal cursors on corners."""
        edge = self._edge_hit(pos)
        if (edge & Qt.Edge.LeftEdge and edge & Qt.Edge.TopEdge) \
                or (edge & Qt.Edge.RightEdge and edge & Qt.Edge.BottomEdge):
            return Qt.CursorShape.SizeFDiagCursor
        if (edge & Qt.Edge.LeftEdge and edge & Qt.Edge.BottomEdge) \
                or (edge & Qt.Edge.RightEdge and edge & Qt.Edge.TopEdge):
            return Qt.CursorShape.SizeBDiagCursor
        if edge & Qt.Edge.LeftEdge or edge & Qt.Edge.RightEdge:
            return Qt.CursorShape.SizeHorCursor
        if edge & Qt.Edge.TopEdge or edge & Qt.Edge.BottomEdge:
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def _update_edge_cursor(self, shape):
        """Show the OS resize cursor while hovering a window edge."""
        if shape is Qt.CursorShape.ArrowCursor:
            self._restore_edge_cursor()
            return
        if self._edge_cursor is shape:
            return
        self._restore_edge_cursor()
        QGuiApplication.setOverrideCursor(QCursor(shape))
        self._edge_cursor = shape

    def _restore_edge_cursor(self):
        """Drop the edge-resize cursor override if one is active."""
        if self._edge_cursor is None:
            return
        self._edge_cursor = None
        QGuiApplication.restoreOverrideCursor()

    def _ensure_player(self):
        if self.player is not None:
            return True
        try:
            try:
                hwnd = int(self._video_container.winId()) or None
            except (Exception,) as e:
                _print(f"[MPV] Embedding unavailable ({e}) — using separate window")
                hwnd = None
            player, self._embedded = playback_controller.create_mpv_player(
                embed_hwnd=hwnd,
                extra_kwargs=self._color_output_kwargs(),
            )
            self.player = player
            self._sync_logged = False
            self._sync_no_child_logged = False
            if self._hdr_enabled:
                src = "HDR" if self._source_is_hdr else "SDR (upconverted)"
                _print(f"[MPV] Player created with HDR output {self._nits}nits (src={src}, embedded={self._embedded})")
            else:
                _print(f"[MPV] Player created with SDR output (embedded={self._embedded})")
            _print("[MPV] Player created via libmpv")
            @self.player.event_callback("end-file")
            def _end_file_handler(event):
                try:
                    data = event.as_dict()
                    reason = data.get("reason") or "unknown"
                    if isinstance(reason, bytes):
                        reason = reason.decode("utf-8", "replace")
                    # Surface the end reason; _on_playback_end decides between
                    # a natural end and bounded auto-recovery.
                    self._playback_end_signal.emit(f"{reason}")
                except (Exception,) as ex:
                    _print(f"[MPV] end-file callback error: {ex}")
            @self.player.event_callback("video-reconfig")
            def _video_reconfig_handler(_event):
                try:
                    self._video_reconfig_signal.emit()
                except (Exception,) as ex:
                    _print(f"[MPV] video-reconfig callback error: {ex}")

            def _rmb_handler(*_args, **_kwargs):
                try:
                    self._fs_menu_signal.emit()
                except (Exception,) as exc:
                    _print(f"[MENU] Fullscreen-menu signal failed: {exc}")
            try:
                # Installed python-mpv: key_binding(keydef, mode) is a
                # decorator factory — apply it to the handler directly.
                self.player.key_binding("MBTN_RIGHT")(_rmb_handler)
            except TypeError:
                try:
                    self.player.register_key_binding("MBTN_RIGHT", _rmb_handler)
                except (Exception,) as e:
                    _print(f"[MPV] Right-click binding unavailable: {e}")
            except (Exception,) as e:
                _print(f"[MPV] Right-click binding unavailable: {e}")
            return True
        except (Exception,) as e:
            _print(f"[MPV] Failed to create player: {e}")
            self._embedded = False
            self.status_label.setText(f"mpv error: {e}")
            return False

    def _persist_settings(self):
        save_settings({"hdr_enabled": self._hdr_enabled, "nits": self._nits,
                       "sync_offset_seconds": self.user_sync_offset,
                       "last_open_dir": self._last_open_dir})

    def _toggle_hdr(self, checked):
        self._hdr_enabled = checked
        self.hdr_btn.setText("HDR: ON" if checked else "HDR: OFF")
        self.nits_spin.setEnabled(checked)
        self._persist_settings()
        self._configure_color_output()
        self._hdr_probe_done = False
        QTimer.singleShot(2000, self._check_hdr_output)
        _print(f"[HDR] Toggle: {'ON' if checked else 'OFF'}")

    def _on_nits_changed(self, value):
        self._nits = value
        self._persist_settings()
        self._configure_color_output()
        _print(f"[HDR] Nits set to {value}")

    def _on_sync_nudge(self, delta):
        self.user_sync_offset = max(-1.0, min(1.0, self.user_sync_offset + delta))
        self.sync_offset_label.setText(f"Video Sync: {float(self.user_sync_offset):+.2f}s")
        self._persist_settings()
        if delta > 0:
            _print(f"[SYNC] {float(self.user_sync_offset):+.2f}s - video delayed by {abs(self.user_sync_offset):.2f}s")
        else:
            _print(f"[SYNC] {float(self.user_sync_offset):+.2f}s - video advanced by {abs(self.user_sync_offset):.2f}s")

    def _on_sync_nudge_plus(self):
        self._on_sync_nudge(0.05)

    def _on_sync_nudge_minus(self):
        self._on_sync_nudge(-0.05)

    def _log_color_info(self):
        """Log actual color state after content is loaded."""
        if not self.player:
            return
        parts = []
        for name in ("current_vo", "target_colorspace_hint",
                      "target_colorspace_hint_mode", "target_prim",
                      "target_trc", "target_peak", "gamut_mapping_mode",
                      "inverse_tone_mapping", "hdr_compute_peak",
                      "colormatrix", "colorlevels",
                      "hwdec_current"):
            try:
                val = getattr(self.player, name)
                parts.append(f"{name}={val}")
            except (Exception,):
                parts.append(f"{name}=n/a")
        for name in ("video-params/primaries", "video-params/gamma",
                     "video-params/sig-peak", "video-params/light",
                     "video-output-params/primaries", "video-output-params/gamma",
                     "video-output-params/sig-peak", "video-output-params/light"):
            try:
                val = playback_controller.get_mpv_property(self.player,name)
                parts.append(f"{name}={val}")
            except (Exception,):
                parts.append(f"{name}=n/a")
        # noinspection PyStringConversionWithoutDunderMethod
        parts.append(f"source_hdr={getattr(self, '_source_is_hdr', None)!s}")
        _print(f"[COLOR] {' '.join(parts)}")

    def get_position(self):
        if not self.player:
            return 0, 0
        try:
            pos = self.player.time_pos
            dur = self.player.duration
            pos_ms = int(pos * 1000) if pos is not None else 0
            dur_ms = int(dur * 1000) if dur is not None else 0
            return pos_ms, dur_ms
        except (Exception,):
            return 0, 0

    def _poll_position(self):
        if not self.player:
            return
        self._check_subtitle_tracks()
        if getattr(self, '_is_live', False):
            self._set_time_text("LIVE / --:--")
            return
        pos_ms, dur_ms = self.get_position()
        if dur_ms > 0:
            self._duration_ms = dur_ms
            for s in (self.seek_slider, self._fs_seek_slider):
                s.blockSignals(True)
                s.setRange(0, dur_ms)
                s.blockSignals(False)
        if pos_ms > 0 and not self.seeking:
            for s in (self.seek_slider, self._fs_seek_slider):
                s.blockSignals(True)
                s.setValue(pos_ms)
                s.blockSignals(False)
            self._set_time_text(f"{fmt_time(pos_ms)} / {fmt_time(self._duration_ms)}")

    def _on_seek_pressed(self):
        self.seeking = True

    def _on_seek_moved(self, value):
        self._set_time_text(f"{fmt_time(value)} / {fmt_time(self._duration_ms)}")

    def _set_time_text(self, text):
        self.time_label.setText(text)
        if getattr(self, "_fs_time_label", None):
            self._fs_time_label.setText(text)

    def on_live_state(self, is_live):
        # GUI-thread slot for resolver-originated live-state updates; the
        # resolver worker must never touch widgets directly.
        self.seek_slider.setEnabled(not is_live)
        if is_live:
            self._set_time_text("LIVE / --:--")

    def _on_seek_released(self, slider=None):
        self.seeking = False
        pos_ms = (slider or self.seek_slider).value()
        self._do_seek(pos_ms)

    def wait_for_mpv_seek(self, target, timeout=6.0):
        """Block until mpv's async seek lands near target (mpv is paused during seeks)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                tp = float(self.player.time_pos) if self.player else None
            except (Exception,):
                tp = None
            if tp is not None and abs(tp - target) <= 0.3:
                return
            time.sleep(0.15)
        _print(f"[SYNC] mpv seek did not settle in time (target={target:.1f}s) — monitor will re-check")

    def read_mpv_property(self, name):
        """Public raw-property accessor for mpv (wraps python-mpv's
        underscored _get_property so UI-free modules like sync_controller
        can query the player through the window)."""
        if not self.player:
            return None
        return playback_controller.get_mpv_property(self.player, name)

    def _do_seek(self, pos_ms):
        if getattr(self, '_is_live', False):
            return
        pos_sec = float(pos_ms) / 1000.0
        self._seek_gen += 1
        gen = self._seek_gen
        self.seek_in_progress = True
        if self.player:
            with self.av_lock:
                try:
                    self.player.pause = True
                    self.player.seek(pos_sec, "absolute")
                    self.player.speed = 1.0
                    self.current_speed = 1.0
                except (Exception,) as e:
                    _print(f"[MPV] Seek error: {e}")
        if not self.current_uri:
            self.seek_in_progress = False
            return

        audio = self._resolved_audio_url

        def _seek_success():
            sync_controller.set_seek_base_pos(pos_sec)
            self.sync_ready = True
            self.initial_sync_done = True
            self.reset_sync_state()
            self.last_soco_elapsed = 0.0
            self.last_soco_time = time.time()
            self.fd_samples = []
            self.sync_offset_runtime = 0.0
            self.stall_active = False
            self.stall_started_at = 0.0
            self.correction_holdoff_until = time.time() + 2.0
            if self.player:
                with self.av_lock:
                    try:
                        base = 1.0 - self.sync_rate_bias
                        self.player.pause = False
                        self.player.speed = base
                        self.current_speed = base
                    except (Exception,) as ex:
                        _print(f"[SYNC] Seek resume error: {ex}")
            _print(f"[SEEK] Done: base={pos_sec:.1f}s, monitor will fine-tune")

        def _seek_fail(message):
            # Keep base/offset consistent so the monitor's auto-restart (todo C)
            # resumes at the right spot and re-anchors when Sonos revives.
            sync_controller.set_seek_base_pos(pos_sec)
            self.sync_ready = True
            self.initial_sync_done = False
            self.reset_sync_state()
            self.last_soco_elapsed = 0.0
            self.last_soco_time = time.time()
            self.correction_holdoff_until = time.time() + 3.0
            if self.player:
                with self.av_lock:
                    try:
                        self.player.pause = False
                    except (Exception,) as exc:
                        _print(f"[SEEK] Resume on failed seek failed: {exc}")
            self._status_signal.emit(message)
            _print(f"[SEEK] Failed: {message}")

        def work():
            def _seek_stale():
                # Beyond _seek_gen: a play/stop (or shutdown) since this seek
                # started must cancel the remaining audio/Sonos side effects.
                return self._seek_gen != gen or not self._playback_is_current(playback_gen)

            try:
                if _seek_stale():
                    return
                if not audio:
                    _print("[SEEK] No resolved audio URL, video-only seek")
                    self.sync_ready = True
                    return
                headers = getattr(self, '_resolved_audio_headers', None)
                # Land the video seek FIRST so Sonos doesn't get a head start
                # while mpv is still decoding into the seek target.
                self.wait_for_mpv_seek(pos_sec)
                if _seek_stale():
                    return
                set_audio_stream(audio, seek_sec=pos_sec, headers=headers)
                if _seek_stale():
                    return
                start_sonos_stream()
                status = wait_for_sonos_audio()
                if _seek_stale():
                    return
                if status != "ok" and audio.startswith("http") and not stream_server.get_output_seek_only():
                    err_tail = active_stream_stderr_tail()
                    if "403" in err_tail or "Forbidden" in err_tail or "HTTP error" in err_tail:
                        _print("[SEEK] ffmpeg HTTP fetch error — output-seek fallback skipped")
                    else:
                        _print("[SEEK] No audio via input seek — retrying once with output seek")
                        stream_server.set_output_seek_only(True)
                        try:
                            set_audio_stream(audio, seek_sec=pos_sec, headers=headers)
                            if _seek_stale():
                                return
                            start_sonos_stream()
                            status = wait_for_sonos_audio()
                            if _seek_stale():
                                return
                        finally:
                            stream_server.set_output_seek_only(False)
                if status == "ok":
                    _seek_success()
                else:
                    _seek_fail("Seek: audio stream unavailable — video continues")
            finally:
                if self._seek_gen == gen:
                    self.seek_in_progress = False

        playback_gen = self._playback_generation
        threading.Thread(target=work, daemon=True).start()

    def browse_file(self):
        start_dir = self._last_open_dir if os.path.isdir(self._last_open_dir) else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Media File", start_dir,
            "Media Files (*.mp4 *.mkv *.avi *.mov *.ts *.m4v *.webm *.flv *.mp3 *.flac *.wav *.ogg);;All Files (*)",
        )
        if path:
            self.uri_label.setText(path)
            self._last_open_dir = os.path.dirname(path)
            self._persist_settings()

    def on_play(self):
        uri = self.uri_label.text().strip()
        if not uri:
            return
        self.do_play_uri(uri)

    def _next_playback_generation(self):
        # Monotonic token: workers capture it at spawn; play/stop/close boundaries
        # advance it so stale workers can be detected and canceled.
        self._playback_generation += 1
        return self._playback_generation

    def _playback_is_current(self, generation):
        # Stale/shutdown guard for worker threads: True only while this exact
        # generation is still the active playback and shutdown wasn't requested.
        return generation == self._playback_generation and not self.shutdown_event.is_set()

    def do_play_uri(self, uri):
        self._next_playback_generation()
        soft = bool(getattr(self, "_soft_restarting", False))
        if not soft:
            self._restart_attempts = 0
            self._force_fresh_resolve = False
            self._pending_start_pos = None
        self.do_stop()
        generation = self._playback_generation  # final bump happens in do_stop
        self.current_uri = uri
        self._is_live = False
        self._resolved_audio_url = None
        self.status_label.setText("Starting...")

        if not self._ensure_player():
            return

        self.status_label.setText("Resolving URL...")
        _print(f"[PLAY] Resolving: {uri}")

        def _resolve_and_play_inner():
            if not self._playback_is_current(generation):
                return  # superseded by a newer play/stop (or shutdown) — stay silent

            start_pos = float(getattr(self, "_pending_start_pos", 0.0) or 0.0)
            force_fresh = bool(getattr(self, "_force_fresh_resolve", False))
            self._pending_start_pos = None
            self._force_fresh_resolve = False
            start_tier_index = int(getattr(self, "_pending_tier_index", 0) or 0)
            self._pending_tier_index = 0

            # Defensive init
            video_headers = {}
            video_candidates = []

            # Check if we have cached probe data from pre-play probing
            use_cache = (not force_fresh
                         and self._probe_data is not None
                         and self._last_probed_uri == uri
                         and self._probe_data.get("audio_tracks"))

            if use_cache and self._probe_data["type"] == "url":
                # Use cached probe data (avoids redundant yt-dlp call)
                data = self._probe_data
                _video_url = data.get("video_url", uri)
                video_headers = data.get("video_headers", {})
                video_candidates = data.get("video_candidates", [])
                title = data.get("title", uri)
                is_live = data.get("is_live", False) or False
                tracks = data.get("audio_tracks", [])
                sel = self._selected_audio_index
                if 0 <= sel < len(tracks):
                    audio_url = tracks[sel].get("url", uri)
                    audio_headers = tracks[sel].get("http_headers", {})
                elif tracks:
                    audio_url = tracks[0].get("url", uri)
                    audio_headers = tracks[0].get("http_headers", {})
                else:
                    audio_url = uri
                    audio_headers = {}
                _print(f"[PLAY] Using cached probe data: {title}")
            elif use_cache and self._probe_data["type"] == "local":
                # Local file: use raw URI; track index from selected_audio_track
                _video_url = uri
                audio_url = uri
                title: str = os.path.basename(uri)  # type: ignore
                is_live = False
                audio_headers = {}
                _print(f"[PLAY] Local file with cached probe: {title}")
            else:
                _video_url, audio_url, title, _duration, is_live, video_headers, audio_headers, video_candidates = resolve_media_url(uri)

                if audio_url is None:
                    code = media.get_last_yt_error()
                    if code is not None:
                        _print(f"[PLAY] Resolve blocked: {yt_error_text(code)}")
                        if self._playback_is_current(generation):
                            self._status_signal.emit("Can't play: " + yt_error_text(code))
                        return

                # Step 2: If yt-dlp failed, scrape the page HTML directly
                if audio_url is None:
                    _print("[PLAY] yt-dlp failed, scraping page HTML...")
                    scraped = scrape_page_for_media(uri)
                    if scraped:
                        audio_url = scraped
                        _print(f"[PLAY] Scrape found: {audio_url[:120]}")

                # Step 3: Fallback to raw URI
                if audio_url is None:
                    audio_url = uri
                    title = title or uri
                    is_live = False
                    _print("[PLAY] Could not resolve media, using raw URI")
                else:
                    _print(f"[PLAY] Final audio URL: {audio_url[:120]}...")

            if not self._playback_is_current(generation):
                return  # playback moved on during resolution — discard results
            self._resolved_audio_url = audio_url
            self._resolved_audio_headers = audio_headers
            self._is_live = is_live
            if video_candidates:
                self._resolved_video_candidates = list(video_candidates)
                if 0 < start_tier_index < len(video_candidates):
                    video_candidates = video_candidates[start_tier_index:]
                elif start_tier_index >= len(video_candidates):
                    video_candidates = []

            # Source HDR from probe data; a candidate winner overrides below.
            self._source_is_hdr = False
            if self._probe_data:
                self._source_is_hdr = self._probe_data.get("is_hdr", False)
            self._aspect_fit_pending = True
            self._aspect_fit_pending_at = time.time()

            self.live_state_signal.emit(is_live)
            if is_live:
                _print(f"[PLAY] Live stream detected: {title}")

            if not self._playback_is_current(generation):
                return
            try:
                self.player.pause = True
                self.player.speed = 1.0
                self.current_speed = 1.0
                try:
                    self.player.ytdl_format = (
                        "bestvideo+bestaudio/best"
                        if self._source_is_hdr
                        else "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestaudio/best"
                    )
                except (Exception,) as e:
                    _print(f"[PLAY] ytdl_format override failed: {e}")
                winner = None
                if not is_live and video_candidates:
                    for cand_idx, cand in enumerate(video_candidates):
                        if not self._playback_is_current(generation):
                            return
                        try:
                            hdr_fields = [f"{k}: {v}" for k, v in (cand.http_headers or {}).items()]
                            if not any(k.lower() == "user-agent" for k in (cand.http_headers or {})):
                                hdr_fields.append(
                                    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
                            playback_controller.set_mpv_property(self.player,"http-header-fields", hdr_fields)
                        except (Exception,) as e:
                            _print(f"[PLAY] Could not set video headers: {e}")
                        self.player.play(cand.url)
                        ready = self._wait_for_video_ready(uri)
                        if not self._playback_is_current(generation):
                            return
                        if ready:
                            ok, reason = self._check_video_startup_health(cand.tier, generation)
                        else:
                            ok, reason = False, "video never decoded"
                        if ok:
                            winner = cand
                            self.current_video_tier_index = start_tier_index + cand_idx
                            _print(f"[PLAY] Video tier {cand.tier} healthy: "
                                  f"{cand.vcodec.split('.')[0]} {cand.height}p")
                            break
                        _print(f"[PLAY] Video tier {cand.tier} unhealthy ({reason}) — trying next candidate")
                        if self._playback_is_current(generation):
                            self._status_signal.emit(f"Video {cand.tier}p unstable — trying lower quality...")
                    if winner is None:
                        _print("[PLAY] No video candidate healthy — legacy fallback")
                if winner is None:
                    self.current_video_tier_index = None
                    self._resolved_video_candidates = []
                    direct_video = (isinstance(_video_url, str) and _video_url.startswith("http")
                                    and _video_url != uri)
                    if direct_video:
                        try:
                            hdr_fields = [f"{k}: {v}" for k, v in (video_headers or {}).items()]
                            if not any(k.lower() == "user-agent" for k in (video_headers or {})):
                                hdr_fields.append(
                                    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
                            playback_controller.set_mpv_property(self.player,"http-header-fields", hdr_fields)
                            _print(f"[PLAY] Direct video URL mode ({len(hdr_fields)} header fields)")
                        except (Exception,) as e:
                            _print(f"[PLAY] Could not set video headers: {e}")
                        self.player.play(_video_url)
                        threading.Thread(target=self._video_load_fallback, args=(uri, generation), daemon=True).start()
                    else:
                        self.player.play(uri)
                _print(f"[MPV] Playing: {title}")
            except (Exception,) as e:
                _print(f"[MPV] Play error: {e}")
                if self._playback_is_current(generation):
                    self._status_signal.emit(f"Error: {e}")
                return

            if not self._playback_is_current(generation):
                return
            if winner is not None:
            # noinspection PyUnresolvedReferences
                self._source_is_hdr = winner.is_hdr
            _print(f"[PLAY] Source HDR: {self._source_is_hdr}, HDR toggle: {self._hdr_enabled}")
            self._configure_color_output()
            self._hdr_probe_done = False
            self._schedule_color_log.emit()
            if not self._playback_is_current(generation):
                return
            set_audio_stream(audio_url, seek_sec=start_pos, headers=self._resolved_audio_headers)
            self._wait_for_video_ready(uri)
            if not self._playback_is_current(generation):
                return
            start_sonos_stream()
            status = wait_for_sonos_audio()

            if status != "ok" and not is_live and audio_url.startswith("http"):
                if not self._playback_is_current(generation):
                    return
                _print("[PLAY] Audio stream failed — re-resolving and retrying once")
                _v2, audio_url2, _t2, _d2, _live2, _vh2, audio_headers2, _cands2 = resolve_media_url(uri)
                if audio_url2:
                    if not self._playback_is_current(generation):
                        return
                    self._resolved_audio_url = audio_url2
                    self._resolved_audio_headers = audio_headers2
                    set_audio_stream(audio_url2, seek_sec=start_pos, headers=audio_headers2)
                    if not self._playback_is_current(generation):
                        return
                    start_sonos_stream()
                    status = wait_for_sonos_audio()

            if not self._playback_is_current(generation):
                return
            if is_live:
                self.play_started_at = time.time()
                sync_controller.set_seek_base_pos(0.0)
                self.sync_ready = True
                self.initial_sync_done = True
                self.reset_sync_state()
                self.current_speed = 1.0
                self._status_signal.emit(f"Playing LIVE: {title}")
                if self.player:
                    try:
                        self.player.pause = False
                        self.player.speed = 1.0
                    except (Exception,) as e:
                        _print(f"[PLAY] Live resume failed: {e}")
            elif status == "ok":
                self.play_started_at = time.time()
                sync_controller.set_seek_base_pos(start_pos)
                self.sync_ready = True
                _print("[SYNC] Initial: base=0, monitor will sync")
                self._status_signal.emit(f"Playing: {title}")
                if start_pos > 0 and self.player:
                    try:
                        with self.av_lock:
                            self.player.seek(start_pos, "absolute")
                    except (Exception,) as e:
                        _print(f"[PLAY] Restart video seek failed (monitor will re-anchor): {e}")
            else:
                if start_pos > 0 and self.player:
                    try:
                        with self.av_lock:
                            self.player.seek(start_pos, "absolute")
                    except (Exception,) as e:
                        _print(f"[PLAY] Restart video seek failed: {e}")
                if self.player:
                    try:
                        self.player.pause = False
                    except (Exception,) as e:
                        _print(f"[PLAY] Video-only resume failed: {e}")
                self._status_signal.emit(f"Playing (video only): {title}")

        def resolve_and_play():
            try:
                _resolve_and_play_inner()
            except (Exception,) as e:
                traceback.print_exc()
                if self._playback_is_current(generation):
                    self._status_signal.emit(f"Error: {e}")

        threading.Thread(target=resolve_and_play, daemon=True).start()

    def _video_load_fallback(self, page_uri, generation):
        """If the direct video URL never decodes, fall back to page-URL playback.

        Aborts silently when the playback generation went stale or shutdown
        was requested — a stale fallback must never drive mpv.

        Spawned only from the legacy no-candidate fallback path in the
        resolver (when no health-gated video candidate won or none existed)."""
        deadline = time.time() + 15.0
        while time.time() < deadline:
            time.sleep(0.5)
            if not self._playback_is_current(generation) or self.current_uri != page_uri:
                return  # playback changed — not our concern anymore
            try:
                if self.player and playback_controller.get_mpv_property(self.player,"video-params/w") is not None:
                    return  # direct URL is decoding fine
            except (Exception,) as e:
                _print(f"[PLAY] Video-decode probe failed: {e}")
        if not self._playback_is_current(generation) or self.current_uri != page_uri \
                or not self.player:
            return
        try:
            _print("[PLAY] Direct video URL failed to load — falling back to page playback")
            self.player.play(page_uri)
        except (Exception,) as e:
            _print(f"[PLAY] Fallback playback error: {e}")

    def do_stop(self):
        self._next_playback_generation()
        self._seek_gen += 1
        self.seek_in_progress = False
        stream_server.set_output_seek_only(False)
        sync_controller.set_seek_base_pos(0.0)
        self.current_speed = 1.0
        self.sync_ready = False
        self.initial_sync_done = False
        self.reset_sync_state()
        self.last_soco_elapsed = 0.0
        self.last_soco_time = 0.0
        self.stall_active = False
        self.stall_started_at = 0.0
        self.current_uri = None
        self._is_live = False
        self._source_is_hdr = False
        self._aspect_fit_pending = False
        self.mpv_buffering = False
        self.buffer_episodes = 0
        self._resolved_audio_url = None
        self._resolved_audio_headers = {}
        self._resolved_video_candidates = []
        self.current_video_tier_index = None
        if not getattr(self, "_soft_restarting", False):
            self._pending_tier_index = 0  # survive the do_stop inside a soft-restart play
        self._subtitle_tracks = []
        self._sub_last_track_ids = ()
        self.sub_combo.blockSignals(True)
        self.sub_combo.clear()
        self.sub_combo.addItem("No subtitles", -1)
        self.sub_combo.setEnabled(False)
        self.sub_combo.blockSignals(False)
        self.sub_btn.blockSignals(True)
        self.sub_btn.setChecked(True)
        self.sub_btn.setText("Subs: ON")
        self.sub_btn.blockSignals(False)
        self.seek_slider.setEnabled(True)
        stop_active_stream_session()
        if self.player:
            try:
                self.player.stop()
            except (Exception,) as e:
                _print(f"[PLAY] mpv stop failed: {e}")
        stop_sonos_stream()
        self.status_label.setText("Stopped")
        self._set_time_text("00:00 / 00:00")
        self.seek_slider.setValue(0)

    def on_stop(self):
        self.do_stop()

    def _toggle_maximized(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _sync_max_glyph(self):
        self.max_btn.setText("\uE923" if self.isMaximized() else "\uE922")

    def on_fullscreen(self):
        if self.isFullScreen():
            self._exit_video_fullscreen()
        else:
            self._enter_video_fullscreen()

    def _toggle_pause(self):
        if not self.player:
            return
        try:
            self.player.pause = not self.player.pause
            paused = bool(self.player.pause)
            self._fs_play_btn.setText(chr(0x23F8) if not paused else chr(0x25B6))
            self._fs_play_btn.setToolTip("Pause" if not paused else "Play")
        except (Exception,) as e:
            _print(f"[PLAY] Pause toggle error: {e}")

    def _fs_stop(self):
        self._exit_video_fullscreen()
        self.on_stop()

    def _position_fs_controls(self):
        if not self.isFullScreen():
            return
        self._fs_controls.setGeometry(12, self.height() - 76,
                                      max(self.width() - 24, 200), 64)

    def _fs_controls_tick(self):
        if not self.isFullScreen():
            return
        local = self.mapFromGlobal(QCursor.pos())
        zone_hit = 0 <= local.x() <= self.width() and local.y() >= self.height() - 140
        over_controls = self._fs_controls.isVisible() \
            and self._fs_controls.geometry().contains(local)
        menu_open = bool(getattr(self, "_main_menu", None) and self._main_menu.isVisible())
        if zone_hit or over_controls or menu_open:
            if not self._fs_controls.isVisible():
                self._fs_controls.show()
                self._fs_controls.raise_()
        elif self._fs_controls.isVisible():
            self._fs_controls.hide()
        rdown = bool(ctypes.windll.user32.GetAsyncKeyState(0x02) & 0x8000)  # type: ignore[attr-defined]
        if rdown and not getattr(self, "_rmb_was_down", False) and self.rect().contains(local):
            self._show_fs_menu()
        self._rmb_was_down = rdown

    def _enter_video_fullscreen(self):
        self.title_bar.hide()
        self.content_area.hide()
        self.showFullScreen()
        QTimer.singleShot(0, self._position_fs_controls)
        self._fs_controls_timer.start(50)
        try:
            dwm = ctypes.windll.dwmapi
            none_color = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE — no border
            dwm.DwmSetWindowAttribute(int(self.winId()), 34, ctypes.byref(none_color), 4)  # type: ignore[attr-defined]
            square = ctypes.c_uint(0)  # DWMWCP_DONOTROUND
            dwm.DwmSetWindowAttribute(int(self.winId()), _DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(square), 4)  # type: ignore[attr-defined]
        except (Exception,) as e:
            _print(f"[UI] Fullscreen DWM attributes failed: {e}")

    def _exit_video_fullscreen(self):
        self.showNormal()
        self.title_bar.show()
        self.content_area.show()
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if getattr(self, "_main_menu", None):
            self._main_menu.hide()
        self._fs_controls.hide()
        self._fs_controls_timer.stop()
        try:
            dwm = ctypes.windll.dwmapi
            none_color = ctypes.c_uint(0xFFFFFFFE)  # DWMWA_COLOR_NONE — windowed stays borderless
            dwm.DwmSetWindowAttribute(int(self.winId()), 34, ctypes.byref(none_color), 4)  # type: ignore[attr-defined]
            corner_pref = ctypes.c_uint(0)  # DWMWCP_DONOTROUND
            dwm.DwmSetWindowAttribute(int(self.winId()), _DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner_pref), 4)  # type: ignore[attr-defined]
        except (Exception,) as e:
            _print(f"[UI] Windowed DWM attributes failed: {e}")

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self._exit_video_fullscreen()
            event.accept()
            return
        super().keyPressEvent(event)

    def _open_main_menu(self):
        if getattr(self, "_main_menu", None):
            self._main_menu.hide()
            self._main_menu.popup(self._menu_btn.mapToGlobal(QPoint(0, self._menu_btn.height())))

    def _show_fs_menu(self):
        if not self.isFullScreen():
            return
        menu = getattr(self, "_main_menu", None)
        if not isinstance(menu, QMenu):
            return
        if menu.isVisible():
            return  # already open (dedup: WndProc + mpv binding may both fire)
        menu.popup(QCursor.pos())

    def _initial_volume_reader(self):
        # One-shot: read the current Sonos volume off the GUI thread and
        # publish it via _sonos_vol_signal; failure leaves the default value.
        try:
            with sonos_lock:
                vol = int(speaker.volume)
            self._sonos_vol_signal.emit(vol)
        except (Exception,) as e:
            _print(f"[SONOS] Volume read failed: {e}")

    def on_sonos_volume(self, vol):
        # GUI-thread slot for the initial Sonos volume; skip it if the user
        # has already moved a slider so a late read can't overwrite them.
        if self.user_volume_touched:
            return
        # Initial fetch only: block valueChanged on both sliders so this
        # programmatic update doesn't count as a user touch or queue a
        # redundant Sonos write echoing the speaker's own value.
        self.vol_slider.blockSignals(True)
        self.vol_slider.setValue(vol)
        self.vol_slider.blockSignals(False)
        self.fs_vol_slider.blockSignals(True)
        self.fs_vol_slider.setValue(vol)
        self.fs_vol_slider.blockSignals(False)
        self.vol_label.setText(f"Volume: {vol}")

    def on_volume_changed(self, val):
        src = self.sender()
        self.user_volume_touched = True
        self.vol_label.setText(f"Volume: {val}")
        for other in (self.vol_slider, self.fs_vol_slider):
            if other is src:
                continue
            other.blockSignals(True)
            other.setValue(val)
            other.blockSignals(False)
        self.queue_speaker_volume(val)

    def queue_speaker_volume(self, val):
        # Stage the newest slider value and wake the single volume worker.
        # GUI-thread only; never blocks on Sonos network I/O.
        if self.shutdown_event.is_set():
            return
        with self.vol_lock:
            self.pending_volume = val
        self.vol_event.set()

    def volume_worker(self):
        # Sole writer of speaker.volume (one daemon thread for the window's
        # lifetime, like sync_monitor_loop): after a short debounce it drains
        # the newest staged value, so a drag burst collapses into ordered
        # writes with at most one Sonos request in flight.
        while not self.shutdown_event.is_set():
            self.vol_event.wait()
            if self.shutdown_event.is_set():
                return
            # Debounce: let a burst finish arriving, then send only its
            # newest value; shutdown interrupts this wait too.
            if self.shutdown_event.wait(self.VOLUME_DEBOUNCE_SEC):
                return
            self.vol_event.clear()
            with self.vol_lock:
                val = self.pending_volume
                self.pending_volume = None
            if val is None or self.shutdown_event.is_set():
                continue
            try:
                with sonos_lock:
                    speaker.volume = val
            except (Exception,) as e:
                _print(f"[SONOS] Volume write failed: {e}")

    def _edge_hit(self, pos):
        rect = self.rect()
        m = self._RESIZE_MARGIN
        x, y = pos.x(), pos.y()
        edge = Qt.Edge(0)
        if x < m:
            edge |= Qt.Edge.LeftEdge
        elif x > rect.width() - m:
            edge |= Qt.Edge.RightEdge
        if y < m:
            edge |= Qt.Edge.TopEdge
        elif y > rect.height() - m:
            edge |= Qt.Edge.BottomEdge
        return edge

    def mousePressEvent(self, event):
        if self.isFullScreen():
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self._edge_hit(event.position().toPoint())
            if edge:
                self._resize_edge = edge
                self._resize_start_global = event.globalPosition().toPoint()
                self._resize_start_geom = self.frameGeometry()
                event.accept()
            elif event.position().toPoint().y() < 36:
                self._dragging = True
                self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
                event.accept()
            else:
                self._resize_edge = None

    def mouseMoveEvent(self, event):
        if self.isFullScreen():
            event.accept()
            return
        if getattr(self, '_dragging', False) and self._drag_pos:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
            return
        if self._resize_edge:
            self._apply_resize_drag(event.globalPosition().toPoint())
            event.accept()
            return
        else:
            self._update_resize_cursor(event.globalPosition().toPoint())

    def mouseReleaseEvent(self, event):
        if self.isFullScreen():
            event.accept()
            return
        self._finish_resize()
        self._drag_pos = None
        self._dragging = False

    def closeEvent(self, event):
        # Deterministic, non-blocking shutdown order (no joins/waits on
        # worker threads): 1) flag all guarded workers (playback/seek/monitor),
        # 2) cancel the active stream session and kill its ffmpeg, 3) stop the
        # HTTP server from the GUI thread, 4) drop Sonos, 5) terminate mpv.
        self.shutdown_event.set()
        self.vol_event.set()  # wake the volume worker so it observes shutdown
        self._next_playback_generation()
        stop_active_stream_session()
        shutdown_stream_server()
        stop_sonos_stream()
        if self.player:
            try:
                self.player.terminate()
            except (Exception,) as e:
                _print(f"[MPV] Terminate failed: {e}")
        event.accept()


if __name__ == "__main__":
    require_ffmpeg()
    _print(f"Stream: http://{local_ip()}:{stream_port}/stream.mp3")
    _print(f"Sonos speaker: {sonos_ip or 'not set'}")
    _print(f"Sonos sync offset: {SYNC_OFFSET_SECONDS}s")

    boost_process_priority()

    free_port(stream_port)

    threading.Thread(target=start_stream_server, daemon=True).start()

    qt_app = QApplication(sys.argv)
    qt_app.setStyleSheet(DARK_STYLE)
    main_window = MainWindow()
    main_window.show()
    main_window.resize(960, 600)

    sys.exit(qt_app.exec())
