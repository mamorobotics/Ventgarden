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

    # Apply LC_CTYPE from environment (UTF-8 for Qt), then force numeric C
    # for libmpv float parsing.
    libc.setlocale(locale.LC_CTYPE, b"")
    os.environ["LC_NUMERIC"] = "C"
    libc.setlocale(locale.LC_NUMERIC, b"C")


def _force_numeric_c_locale() -> None:
    libc_path = ctypes.util.find_library("c")
    if not libc_path:
        return
    libc = ctypes.CDLL(libc_path, use_errno=True)
    libc.setlocale.restype = ctypes.c_char_p
    libc.setlocale.argtypes = [ctypes.c_int, ctypes.c_char_p]
    os.environ["LC_NUMERIC"] = "C"
    libc.setlocale(locale.LC_NUMERIC, b"C")


_configure_runtime_locale()

import ctypes, ctypes.util, os
os.environ["DYLD_LIBRARY_PATH"] = "/opt/homebrew/lib"

import mpv
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget




# ---------------------------------------------------------------------------
# Embedded mpv widget
# ---------------------------------------------------------------------------

class MpvWidget(QWidget):
    """
    Hosts mpv directly inside a native QWidget using window-id embedding.
    This is more robust on Linux systems where QOpenGLWidget may be
    unavailable depending on Qt platform backend.

    Low-latency mpv options mirror the recommended ffplay flags:
        -fflags nobuffer -flags low_delay -framedrop -vf setpts=0
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setMinimumSize(640, 480)
        self.player: mpv.MPV | None = None
        self._pending_url: str | None = None

    def _ensure_player(self) -> bool:
        if self.player is not None:
            return True

        wid = int(self.winId())
        if not wid:
            return False

        _force_numeric_c_locale()

        self.player = mpv.MPV(
            wid=wid,
            vo="gpu",
            cache=False,
            cache_pause=False,
            demuxer_max_bytes="128KiB",
            demuxer_readahead_secs=0,
            vd_lavc_threads=1,
            framedrop="vo",
            video_sync="desync",
            profile='low-latency',
            untimed=True,
            hwdec='auto',
            force_window=True,
        )
        return True

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._ensure_player() and self._pending_url:
            self.player.play(self._pending_url)
            self._pending_url = None

    # ------------------------------------------------------------------
    # Stream control
    # ------------------------------------------------------------------

    def play(self, url: str) -> None:
        self._pending_url = url
        if self._ensure_player() and self.player is not None:
            self.player.play(url)
            self._pending_url = None

    def stop(self) -> None:
        if self.player is not None:
            self.player.command("stop")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.stop()
        if self.player is not None:
            self.player.wait_for_shutdown()
            self.player.terminate()
            self.player = None
        event.accept()
