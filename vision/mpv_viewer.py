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

def _configure_runtime_locale() -> None:
    # Qt requires a UTF-8 locale. If the shell locale is missing/non-UTF-8,
    # default to C.UTF-8 for text handling.
    if not any(
        "UTF-8" in os.environ.get(name, "").upper()
        for name in ("LC_ALL", "LC_CTYPE", "LANG")
    ):
        os.environ.setdefault("LANG", "C.UTF-8")
        os.environ.setdefault("LC_CTYPE", "C.UTF-8")

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


_configure_runtime_locale()

import mpv
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QStatusBar,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtCore import Qt, QTimer, QMetaObject
from PySide6.QtGui import QOpenGLContext




# ---------------------------------------------------------------------------
# OpenGL render widget
# ---------------------------------------------------------------------------

class MpvWidget(QOpenGLWidget):
    """
    Hosts an mpv MpvRenderContext that renders directly into this
    widget's OpenGL framebuffer. No CPU frame copies occur.

    Low-latency mpv options mirror the recommended ffplay flags:
        -fflags nobuffer -flags low_delay -framedrop -vf setpts=0
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(640, 480)

        # Keep the ctypes callback alive for the entire lifetime of the GL context.
        self._proc_addr_fn = mpv.MpvGlGetProcAddressFn(self._get_proc_address)

        self.player = mpv.MPV(
            vo="libmpv",               # Embeddable renderer (not a standalone window)

            # ---- low-latency flags ----------------------------------------
            cache=False,               # Disable demuxer cache  (-fflags nobuffer)
            cache_pause=False,
            demuxer_max_bytes="128KiB",
            demuxer_readahead_secs=0,
            vd_lavc_threads=1,         # Single decode thread reduces queue depth
            framedrop="vo",            # Drop to keep real-time (-framedrop)
            video_sync="desync",       # Don't wait for audio clock (setpts=0 equiv)
        )

        self._render_ctx: mpv.MpvRenderContext | None = None

    # ------------------------------------------------------------------
    # OpenGL proc-address callback (called by libmpv during init)
    # ------------------------------------------------------------------

    def _get_proc_address(self, _ctx: object, name: bytes) -> int:
        ctx = QOpenGLContext.currentContext()
        if ctx is None:
            return 0
        addr = ctx.getProcAddress(name)
        # getProcAddress returns an integer pointer value; wrap safely.
        try:
            return ctypes.cast(int(addr), ctypes.c_void_p).value or 0
        except (TypeError, ctypes.ArgumentError):
            return 0

    # ------------------------------------------------------------------
    # Qt OpenGL lifecycle
    # ------------------------------------------------------------------

    def _schedule_update(self) -> None:
        """Called from mpv's render thread. Marshals to the Qt main thread."""
        QMetaObject.invokeMethod(self, "update", Qt.ConnectionType.QueuedConnection)

    def initializeGL(self) -> None:
        self._render_ctx = mpv.MpvRenderContext(
            self.player,
            "opengl",
            opengl_init_params={"get_proc_address": self._proc_addr_fn},
        )
        # update_cb fires from mpv's render thread — must marshal to main thread.
        self._render_ctx.update_cb = self._schedule_update

    def paintGL(self) -> None:
        if self._render_ctx is None:
            return
        ratio = self.devicePixelRatio()
        self._render_ctx.render(
            flip_y=True,
            opengl_fbo={
                "fbo": self.defaultFramebufferObject(),
                "w":   int(self.width()  * ratio),
                "h":   int(self.height() * ratio),
            },
        )

    def resizeGL(self, _w: int, _h: int) -> None:
        # mpv reads fbo dimensions from paintGL on every frame — nothing to do.
        pass

    # ------------------------------------------------------------------
    # Stream control
    # ------------------------------------------------------------------

    def play(self, url: str) -> None:
        self.player.play(url)

    def stop(self) -> None:
        self.player.command("stop")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        # Order matters to avoid a segfault:
        # 1. Stop playback so mpv's render thread goes idle.
        # 2. Free the render context (detaches from the GL context).
        # 3. Terminate the mpv core.
        # 4. Only then let Qt destroy the GL widget.
        self.stop()
        self.player.wait_for_shutdown()
        if self._render_ctx is not None:
            self._render_ctx.free()
            self._render_ctx = None
        self.player.terminate()
        event.accept()
