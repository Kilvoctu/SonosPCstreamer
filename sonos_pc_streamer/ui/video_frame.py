"""Native Qt widget that mpv renders into via wid embedding."""

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget


class VideoFrame(QWidget):
    """Native widget that mpv renders into via wid embedding."""

    def __init__(self):
        super().__init__()
        self.setObjectName("videoFrame")
        self.setStyleSheet("background-color: #000000;")
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        self.setAttribute(Qt.WidgetAttribute.WA_PaintOnScreen)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setMinimumHeight(180)
        self.winId()  # force native window creation now
        self._sync_cb: Callable[[], None] | None = None
        self.setMouseTracking(True)
        self._ui_cb: Callable[[str, object], None] | None = None

    def set_sync_callback(self, cb):
        self._sync_cb = cb

    def set_ui_callback(self, cb):
        self._ui_cb = cb

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._sync_cb:
            QTimer.singleShot(0, self._sync_cb)

    def showEvent(self, event):
        super().showEvent(event)
        if self._sync_cb:
            QTimer.singleShot(0, self._sync_cb)

    def mousePressEvent(self, event):
        if self._ui_cb:
            self._ui_cb("press", event)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._ui_cb:
            self._ui_cb("move", event)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._ui_cb:
            self._ui_cb("release", event)
        event.accept()

    def leaveEvent(self, event):
        if self._ui_cb:
            self._ui_cb("leave", event)
        event.accept()
