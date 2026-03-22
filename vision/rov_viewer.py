"""
ROV Camera Viewer
-----------------
Embeds mpv's OpenGL renderer into a Qt window using libmpv's render
context API. GPU rendering is fully preserved — decoded JPEG frames go
straight from FFmpeg's decoder to an OpenGL texture without passing
through CPU memory.

Auto-reconnect: a 2-second heartbeat timer re-issues play() whenever
mpv goes idle while streaming should be active (cable blip, ROV restart).

Requirements:
    pip install -r requirements.txt

    macOS also needs libmpv:
        brew install mpv

Usage:
    python rov_viewer.py
    python rov_viewer.py http://192.168.1.50:8080/stream
"""

import sys
import os
import ctypes
import ctypes.util
import locale
import argparse
import subprocess

def _configure_runtime_locale() -> None:
    def _has_utf8(value: str) -> bool:
        upper_value = value.upper()
        return "UTF-8" in upper_value or "UTF8" in upper_value

    def _normalize_locale_name(value: str) -> str:
        return "".join(ch for ch in value.lower() if ch.isalnum())

    def _detect_utf8_locale() -> str | None:
        preferred = ["C.UTF-8", "en_US.UTF-8", "en_GB.UTF-8"]
        try:
            result = subprocess.run(
                ["locale", "-a"],
                capture_output=True,
                text=True,
                check=False,
            )
            available = {line.strip() for line in result.stdout.splitlines() if line.strip()}
            normalized = {_normalize_locale_name(entry): entry for entry in available}
            for candidate in preferred:
                match = normalized.get(_normalize_locale_name(candidate))
                if match:
                    return match
            for entry in available:
                if _has_utf8(entry):
                    return entry
        except Exception:
            return None
        return None

    effective_locale = os.environ.get("LC_ALL") or os.environ.get("LC_CTYPE") or os.environ.get("LANG", "")
    if not _has_utf8(effective_locale):
        utf8_locale = _detect_utf8_locale()
        if utf8_locale:
            os.environ["LANG"] = utf8_locale
            os.environ["LC_CTYPE"] = utf8_locale
            if os.environ.get("LC_ALL"):
                os.environ["LC_ALL"] = utf8_locale

    libc_path = ctypes.util.find_library("c")
    if not libc_path:
        return

    libc = ctypes.CDLL(libc_path, use_errno=True)
    libc.setlocale.restype = ctypes.c_char_p
    libc.setlocale.argtypes = [ctypes.c_int, ctypes.c_char_p]
    libc.setlocale(locale.LC_CTYPE, b"")
    os.environ["LC_NUMERIC"] = "C"
    libc.setlocale(locale.LC_NUMERIC, b"C")


_configure_runtime_locale()

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QStatusBar,
)
from PySide6.QtCore import QTimer

from mpv_viewer import MpvWidget

# Default URL — override via CLI or the UI text box

DEFAULT_URL = "http://192.168.1.100:8080/stream"


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ROVViewer(QMainWindow):
    def __init__(self, initial_url: str = DEFAULT_URL) -> None:
        super().__init__()
        self.setWindowTitle("ROV Camera Viewer")
        self._current_url = initial_url
        self._stream_active = False

        # ---- Video widget ------------------------------------------------
        self._video = MpvWidget(self)

        # ---- Controls bar ------------------------------------------------
        self._url_edit = QLineEdit(initial_url)
        self._url_edit.setPlaceholderText("http://<ROV-IP>:8080/stream")
        self._url_edit.returnPressed.connect(self._on_connect)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect)

        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.clicked.connect(self._on_disconnect)
        self._disconnect_btn.setEnabled(False)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Stream URL:"))
        controls.addWidget(self._url_edit, stretch=1)
        controls.addWidget(self._connect_btn)
        controls.addWidget(self._disconnect_btn)

        # ---- Layout ------------------------------------------------------
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self._video, stretch=1)
        layout.addLayout(controls)
        self.setCentralWidget(central)

        # ---- Status bar --------------------------------------------------
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._status.showMessage("Disconnected")

        # ---- Reconnect heartbeat ----------------------------------------
        # Every 2 s: if we want to be streaming but mpv went idle
        # (cable blip, ROV reboot), re-issue play().
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(2000)
        self._heartbeat.timeout.connect(self._check_connection)

        self.resize(900, 600)

    # ------------------------------------------------------------------
    # Slot implementations
    # ------------------------------------------------------------------

    def _on_connect(self) -> None:
        url = self._url_edit.text().strip()
        if not url:
            return
        self._current_url = url
        self._stream_active = True
        self._video.play(url)
        self._heartbeat.start()

        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._status.showMessage(f"Connecting → {url}")

        # Watch mpv's idle property to update the status bar.
        @self._video.player.property_observer("idle-active")
        def _on_idle(name, idle):     # noqa: ARG001
            if not self._stream_active:
                return
            if idle:
                self._status.showMessage("⚠  Stream lost — reconnecting…")
            else:
                self._status.showMessage(f"● Live: {self._current_url}")

    def _on_disconnect(self) -> None:
        self._stream_active = False
        self._heartbeat.stop()
        self._video.stop()

        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._status.showMessage("Disconnected")

    def _check_connection(self) -> None:
        """Reconnect if mpv went idle while we expected it to be playing."""
        if not self._stream_active:
            return
        try:
            idle = self._video.player.idle_active
        except Exception:
            idle = True  # can't query → assume disconnected

        if idle:
            self._video.play(self._current_url)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ROV Camera Viewer")
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_URL,
        help="uStreamer MJPEG URL (default: %(default)s)",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("ROV Viewer")

    window = ROVViewer(initial_url=args.url)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
