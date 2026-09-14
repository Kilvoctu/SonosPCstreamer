"""Dark Qt stylesheet for the Sonos PC Streamer GUI."""


DARK_STYLE = """
QMainWindow, QWidget#centralWidget {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #23262b, stop:1 #101215);
    border: 1px solid;
    border-top-color: #4a4f58;
    border-left-color: #4a4f58;
    border-bottom-color: #07080a;
    border-right-color: #07080a;
}
QLabel {
    color: #8a919b;
    font-size: 11px;
    background: transparent;
}
QWidget#titleBar {
    background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #2b2f35, stop:1 #191c20);
    border-bottom: 1px solid #000000;
}
QWidget#titleBar QLabel {
    color: #c9d1dc;
    font-size: 13px;
    font-weight: 800;
    letter-spacing: 1px;
}
QPushButton {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3a3f46, stop:1 #22262b);
    color: #c8cdd4;
    border: 1px solid;
    border-top-color: #4a4f58;
    border-left-color: #4a4f58;
    border-bottom-color: #07080a;
    border-right-color: #07080a;
    border-radius: 3px;
    padding: 4px 10px;
    font-size: 12px;
    font-weight: 500;
}
QPushButton:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #464c55, stop:1 #282d33);
    color: #e8eaed;
}
QPushButton:pressed {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #22262b, stop:1 #3a3f46);
    border-top-color: #07080a;
    border-left-color: #07080a;
    border-bottom-color: #3a4048;
    border-right-color: #3a4048;
}
QPushButton:disabled {
    background-color: #14171c;
    color: #4a4f58;
    border-top-color: #07080a;
    border-left-color: #07080a;
    border-bottom-color: #1c2027;
    border-right-color: #1c2027;
}
QPushButton#playBtn, QPushButton#stopBtn {
    min-height: 22px;
    font-size: 15px;
    font-weight: bold;
}
QPushButton#playBtn:hover {
    color: #46ff6e;
}
QPushButton#playBtn:pressed {
    color: #46ff6e;
}
QPushButton#stopBtn {
    color: #ff5d73;
}
QPushButton#stopBtn:hover {
    color: #ff7d7d;
}
QPushButton#minBtn, QPushButton#maxBtn, QPushButton#closeBtn {
    background-color: transparent;
    border: none;
    border-radius: 3px;
    color: #9aa3ad;
    font-size: 13px;
    padding: 0px;
}
QPushButton#minBtn:hover, QPushButton#maxBtn:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3a3f46, stop:1 #22262b);
    border: 1px solid;
    border-top-color: #4a4f58;
    border-left-color: #4a4f58;
    border-bottom-color: #07080a;
    border-right-color: #07080a;
    color: #e8eaed;
}
QPushButton#closeBtn:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #5a1c26, stop:1 #33101a);
    border: 1px solid;
    border-top-color: #4a4f58;
    border-left-color: #4a4f58;
    border-bottom-color: #07080a;
    border-right-color: #07080a;
    color: #ff5d73;
}
QPushButton#hdrBtn, QPushButton#subBtn {
    padding: 6px 12px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.5px;
}
QPushButton#hdrBtn:checked, QPushButton#subBtn:checked {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #0e1a12, stop:1 #0b1410);
    color: #46ff6e;
    border-top-color: #07080a;
    border-left-color: #07080a;
    border-bottom-color: #3a4048;
    border-right-color: #3a4048;
    font-weight: bold;
}
QLineEdit {
    background-color: #0b0d10;
    color: #c8cdd4;
    border: 1px solid;
    border-top-color: #07080a;
    border-left-color: #07080a;
    border-bottom-color: #3a4048;
    border-right-color: #3a4048;
    border-radius: 3px;
    padding: 7px 11px;
    font-size: 12px;
    selection-background-color: #2a5a38;
    selection-color: #e8eaed;
}
QLineEdit:focus {
    border: 1px solid #2ea855;
}
QComboBox {
    background-color: #0b0d10;
    color: #c8cdd4;
    border: 1px solid;
    border-top-color: #07080a;
    border-left-color: #07080a;
    border-bottom-color: #3a4048;
    border-right-color: #3a4048;
    border-radius: 3px;
    padding: 5px 10px;
    font-size: 12px;
    min-height: 20px;
}
QComboBox:hover {
    background-color: #12151a;
}
QComboBox:focus {
    border: 1px solid #2ea855;
}
QComboBox:on {
    background-color: #12151a;
}
QComboBox:disabled {
    background-color: #101215;
    color: #4a4f58;
    border-top-color: #07080a;
    border-left-color: #07080a;
    border-bottom-color: #1c2027;
    border-right-color: #1c2027;
}
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 24px;
    border: 1px solid;
    border-top-color: #4a4f58;
    border-left-color: #4a4f58;
    border-bottom-color: #07080a;
    border-right-color: #07080a;
    border-radius: 2px;
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3a3f46, stop:1 #22262b);
}
QComboBox::down-arrow {
    width: 0px;
    height: 0px;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid #8a919b;
}
QComboBox QAbstractItemView {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #1c2026, stop:1 #101215);
    color: #c8cdd4;
    border: 1px solid;
    border-top-color: #4a4f58;
    border-left-color: #4a4f58;
    border-bottom-color: #07080a;
    border-right-color: #07080a;
    border-radius: 3px;
    selection-background-color: #3a3323;
    selection-color: #e8d9ae;
    outline: none;
    padding: 4px;
}
QSpinBox {
    background-color: #0b0d10;
    color: #c8cdd4;
    border: 1px solid;
    border-top-color: #07080a;
    border-left-color: #07080a;
    border-bottom-color: #3a4048;
    border-right-color: #3a4048;
    border-radius: 3px;
    padding: 4px 6px;
    font-size: 12px;
}
QSpinBox:focus {
    border: 1px solid #2ea855;
}
QSpinBox:disabled {
    background-color: #101215;
    color: #4a4f58;
    border-top-color: #07080a;
    border-left-color: #07080a;
    border-bottom-color: #1c2027;
    border-right-color: #1c2027;
}
QSpinBox::up-button, QSpinBox::down-button {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3a3f46, stop:1 #22262b);
    border: 1px solid;
    border-top-color: #4a4f58;
    border-left-color: #4a4f58;
    border-bottom-color: #07080a;
    border-right-color: #07080a;
    border-radius: 2px;
    width: 16px;
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #464c55, stop:1 #282d33);
}
QSlider {
    min-height: 22px;
}
QSlider::groove:horizontal {
    height: 6px;
    background: #0b0d10;
    border: 1px solid;
    border-top-color: #07080a;
    border-left-color: #07080a;
    border-bottom-color: #3a4048;
    border-right-color: #3a4048;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #1f7a3a, stop:1 #2ea855);
    border-radius: 2px;
}
QSlider::add-page:horizontal {
    background: #1a1d22;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    width: 14px;
    height: 18px;
    margin: -6px 0;
    border: 1px solid;
    border-top-color: #4a4f58;
    border-left-color: #4a4f58;
    border-bottom-color: #07080a;
    border-right-color: #07080a;
    border-radius: 3px;
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #6a7078, stop:1 #3a3f46);
}
QSlider::handle:horizontal:hover {
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #7d848d, stop:1 #464c55);
}
QSlider::groove:horizontal:hover {
    background: #12151a;
}
QPushButton#menuBtn {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3a3f46, stop:1 #22262b);
    color: #c8cdd4;
    border: 1px solid;
    border-top-color: #4a4f58;
    border-left-color: #4a4f58;
    border-bottom-color: #07080a;
    border-right-color: #07080a;
    border-radius: 3px;
    font-size: 13px;
    padding: 0px;
}
QPushButton#menuBtn:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #464c55, stop:1 #282d33);
    color: #e8eaed;
}
QMenu {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #23262b, stop:1 #101215);
    color: #c8cdd4;
    border: 1px solid;
    border-top-color: #4a4f58;
    border-left-color: #4a4f58;
    border-bottom-color: #07080a;
    border-right-color: #07080a;
    border-radius: 3px;
    padding: 4px;
    font-size: 12px;
}
QMenu::item {
    padding: 5px 18px;
    border-radius: 2px;
    background: transparent;
}
QMenu::item:selected {
    background-color: #3a3323;
    color: #e8d9ae;
}
QMenu::item:disabled {
    color: #4a4f58;
}
QMenu::separator {
    height: 1px;
    background: #0a0c0e;
    margin: 4px 8px;
}
QWidget#fsControls {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #23262b, stop:1 #101215);
    border: 1px solid;
    border-top-color: #4a4f58;
    border-left-color: #4a4f58;
    border-bottom-color: #07080a;
    border-right-color: #07080a;
    border-radius: 3px;
}
QLabel#statusLabel {
    color: #d9a53f;
    font-size: 12px;
    letter-spacing: 0.5px;
}
QLabel#lcdGreen {
    font-family: "Consolas";
    font-size: 15px;
    font-weight: bold;
    color: #46ff6e;
    background-color: #04120a;
    border: 1px solid;
    border-top-color: #05070a;
    border-left-color: #05070a;
    border-bottom-color: #3a4048;
    border-right-color: #3a4048;
    padding: 2px 8px;
}
QLabel#lcdAmber {
    font-family: "Consolas";
    font-size: 15px;
    font-weight: bold;
    color: #ffb454;
    background-color: #140d03;
    border: 1px solid;
    border-top-color: #05070a;
    border-left-color: #05070a;
    border-bottom-color: #3a4048;
    border-right-color: #3a4048;
    padding: 2px 8px;
}
"""
