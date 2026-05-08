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

import os
import ctypes
import ctypes.util
import locale
import platform
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


def _configure_mpv_library_path() -> None:
    """Help python-mpv find Homebrew's libmpv on macOS."""
    if platform.system() != "Darwin":
        return

    for brew_lib in ("/opt/homebrew/lib", "/usr/local/lib"):
        if os.path.exists(os.path.join(brew_lib, "libmpv.dylib")):
            current = os.environ.get("DYLD_LIBRARY_PATH", "")
            paths = [path for path in current.split(os.pathsep) if path]
            if brew_lib not in paths:
                os.environ["DYLD_LIBRARY_PATH"] = os.pathsep.join([brew_lib, *paths])
            return


_configure_runtime_locale()
_configure_mpv_library_path()

import mpv
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QOpenGLContext
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QWidget


class _RenderUpdateBridge(QObject):
    update_requested = Signal()


# ---------------------------------------------------------------------------
# Embedded mpv widget
# ---------------------------------------------------------------------------

class MpvWidget(QOpenGLWidget):
    """
    Renders mpv directly into Qt's OpenGL framebuffer.

    This uses libmpv's render context instead of mpv's ``wid`` option. On
    macOS, ``wid`` often creates a separate native mpv window; with this
    widget Qt owns the surface and mpv draws into it.

    Low-latency mpv options mirror the recommended ffplay flags:
        -fflags nobuffer -flags low_delay -framedrop -vf setpts=0
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(640, 480)
        self.player: mpv.MPV | None = None
        self._render_context: mpv.MpvRenderContext | None = None
        self._pending_url: str | None = None
        self._get_proc_address_cb = mpv.MpvGlGetProcAddressFn(self._get_proc_address)
        self._update_bridge = _RenderUpdateBridge(self)
        self._update_bridge.update_requested.connect(self.update)

    def _ensure_player(self) -> bool:
        if self.player is not None:
            return True

        if not self.context():
            return False

        _force_numeric_c_locale()

        self.player = mpv.MPV(
            vo="libmpv",
            cache=False,
            cache_pause=False,
            demuxer_max_bytes="128KiB",
            demuxer_readahead_secs=0,
            vd_lavc_threads=1,
            framedrop="vo",
            video_sync="desync",
            profile="low-latency",
            untimed=True,
            hwdec="auto",
        )
        self._render_context = mpv.MpvRenderContext(
            self.player,
            "opengl",
            opengl_init_params={"get_proc_address": self._get_proc_address_cb},
        )
        self._render_context.update_cb = self._request_update
        return True

    def _get_proc_address(self, _ctx, name) -> int:
        context = QOpenGLContext.currentContext()
        if context is None:
            return 0

        address = context.getProcAddress(name)
        if not address:
            return 0

        try:
            return int(address)
        except TypeError:
            return int(ctypes.cast(address, ctypes.c_void_p).value or 0)

    def _request_update(self) -> None:
        self._update_bridge.update_requested.emit()

    def initializeGL(self) -> None:
        if self._ensure_player() and self._pending_url:
            self.player.play(self._pending_url)
            self._pending_url = None

    def paintGL(self) -> None:
        if self._render_context is None:
            return

        self._render_context.update()
        ratio = self.devicePixelRatioF()
        width = int(self.width() * ratio)
        height = int(self.height() * ratio)
        self._render_context.render(
            opengl_fbo={
                "fbo": self.defaultFramebufferObject(),
                "w": width,
                "h": height,
                "internal_format": 0,
            },
            flip_y=True,
        )
        self._render_context.report_swap()

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

    def shutdown(self) -> None:
        if self._render_context is not None:
            self.makeCurrent()
            self._render_context.update_cb = None
            self._render_context.free()
            self._render_context = None
            self.doneCurrent()

        if self.player is None:
            return

        self.stop()
        self.player.terminate()
        self.player.wait_for_shutdown()
        self.player = None

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self.shutdown()
        event.accept()
