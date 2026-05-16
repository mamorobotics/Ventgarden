"""
rov_dual_viewer_x_recorder.py
------------------------------
ROV Camera Viewer + Recorder — combined dual camera edition.

Both cameras share the same IP address but stream on different ports.
Each camera panel has its own:
  • MpvWidget  (independent GPU-accelerated OpenGL renderer)
  • Live status label + auto-reconnect heartbeat
  • Connect / Disconnect buttons
  • ● Record / ■ Stop buttons with elapsed timer
  • 📷 Snapshot button (fetches a still frame via ustreamer /snapshot)
  • Output filename display + ▶ Open button after save

A shared controls bar at the bottom holds:
  • ROV IP and per-camera port fields
  • Save directory picker
  • ▶ Connect Both / ■ Disconnect Both
  • ● Record Both / ■ Stop Both
  • 📷 Snapshot Both

Layout:
    ┌──────────────────────────────────────────────────────────────┐
    │  ┌────────── Camera 1 ──────────┐  ┌────────── Camera 2 ───┐ │
    │  │       MpvWidget              │  │       MpvWidget       │ │
    │  │  status: ● Live              │  │  status: ● Live       │ │
    │  │  [Connect]  [Disconnect]     │  │  [Connect] [Disconn]  │ │
    │  │  ──────────────────────────  │  │  ─────────────────── │ │
    │  │  ● Recording  00:12          │  │  ● Recording  00:12  │ │
    │  │  File: cam1_20250101_...mp4  │  │  File: cam2_...mp4   │ │
    │  │  [● Record]  [■ Stop] [📷]   │  │ [● Record][■ Stop][📷]│ │
    │  └──────────────────────────────┘  └──────────────────────┘ │
    │  ROV IP:[___] Cam1:[__] Cam2:[__]  Save:[__________][Browse]│
    │  [▶ Connect Both] [■ Disconnect Both]                        │
    │  [● Record Both]  [■ Stop Both]  [📷 Snapshot Both]          │
    └──────────────────────────────────────────────────────────────┘

Requirements:
    pip install python-mpv PySide6
    macOS:  brew install mpv ffmpeg
    Linux:  sudo apt install libmpv-dev ffmpeg

Usage:
    python rov_dual_viewer_x_recorder.py
    python rov_dual_viewer_x_recorder.py --ip 192.168.1.50 --port1 8080 --port2 8081
"""

import sys
import os
import ctypes
import ctypes.util
import locale
import argparse
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

# Fix libmpv float parsing before the library loads.
os.environ["LC_NUMERIC"] = "C"
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
_libc.setlocale.restype = ctypes.c_char_p
_libc.setlocale(locale.LC_NUMERIC, b"C")

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout,
    QPushButton, QLineEdit, QLabel, QStatusBar,
    QFileDialog, QFrame,
)
from PySide6.QtCore import Qt, QTimer, QThread, Signal, QUrl
from PySide6.QtGui import QFont, QDesktopServices

from mpv_viewer import MpvWidget


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_IP    = "192.168.1.100"
DEFAULT_PORT1 = 8080
DEFAULT_PORT2 = 8081
STREAM_PATH   = "/stream"
SNAPSHOT_PATH = "/snapshot"   # ustreamer still-frame endpoint

# Use the current working directory as the default save location so it always
# exists and is writable regardless of the OS / user profile layout.
DEFAULT_SAVE  = str(Path.cwd() / "recordings")


# ---------------------------------------------------------------------------
# FFmpeg recording worker
# ---------------------------------------------------------------------------

class RecordingWorker(QThread):
    """
    Runs FFmpeg in a background thread to record an MJPEG stream to MP4.

    Key FFmpeg flags for MJPEG streams
    ------------------------------------
    -use_wallclock_as_timestamps 1
        MJPEG streams from cheap cameras often carry no DTS/PTS timestamps.
        Without this flag FFmpeg calculates duration from the *number of
        frames* it has buffered, which makes a 5-second clip appear as a
        46-second file (because the internal frame counter runs at a
        different rate than wall time).  Forcing wall-clock timestamps makes
        the output duration match real elapsed time.

    -r 30
        Explicitly tells FFmpeg to expect (and emit) 30 fps.  Remove or
        adjust if your camera runs at a different frame rate.

    -vsync cfr
        Constant-frame-rate output — prevents duplicate/dropped frames from
        causing choppiness in the saved file.

    Pipe deadlock fix
    ------------------
    FFmpeg writes verbose progress/log output to stderr continuously. If the
    parent process never reads from the stderr pipe, the OS pipe buffer fills
    up (~64 KB on Linux), FFmpeg blocks trying to write, and it never exits —
    so our wait(timeout=5) fires, kill() is called, and the output file is
    left incomplete/corrupt.

    The fix: drain stderr on a dedicated daemon thread the whole time FFmpeg
    is running. We accumulate the last chunk so we can report errors, but we
    never let the buffer stall FFmpeg.
    """

    status_changed = Signal(str)
    error_occurred = Signal(str)
    finished_ok    = Signal(str)   # emits the output path on clean finish
    finished       = Signal()

    def __init__(self, url: str, output_path: str, parent=None) -> None:
        super().__init__(parent)
        self.url             = url
        self.output_path     = output_path
        self._process        = None
        self._stop_requested = False
        self._stderr_tail    = b""   # last bytes of stderr for error reporting

    # ------------------------------------------------------------------
    # Internal: drain stderr so the pipe never stalls FFmpeg
    # ------------------------------------------------------------------

    def _drain_stderr(self) -> None:
        """
        Runs on a daemon thread. Reads stderr line-by-line until EOF so the
        OS pipe buffer never fills up and blocks FFmpeg. Keeps only the last
        4 KB for error reporting; we don't need the full log in memory.
        """
        try:
            for line in self._process.stderr:
                self._stderr_tail = (self._stderr_tail + line)[-4096:]
        except Exception:
            pass  # process already gone — nothing to drain

    # ------------------------------------------------------------------
    # QThread entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        cmd = [
            "ffmpeg", "-y",

            # ---- Input timing fix for timestamp-free MJPEG streams ----
            "-use_wallclock_as_timestamps", "1",
            "-fflags", "nobuffer+genpts",
            "-flags",  "low_delay",

            "-i", self.url,

            # ---- Output encoding ----------------------------------------
            "-r",     "30",         # match your camera's actual frame rate
            "-vsync", "cfr",        # constant-frame-rate muxing
            "-c:v",   "libx264",
            "-preset", "ultrafast",
            "-crf",   "23",
            "-movflags", "+faststart",
            "-an",                  # no audio
            self.output_path,
        ]
        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,  # we never need stdout
                stderr=subprocess.PIPE,
            )

            # Drain stderr on a background daemon thread to prevent the OS
            # pipe buffer from filling up and deadlocking FFmpeg.
            drain_thread = threading.Thread(
                target=self._drain_stderr,
                daemon=True,
                name="ffmpeg-stderr-drain",
            )
            drain_thread.start()

            self.status_changed.emit(f"● Recording → {Path(self.output_path).name}")

            # Block until FFmpeg exits (normally or after stop() signals it).
            self._process.wait()

            # Make sure the drain thread has finished before we inspect stderr.
            drain_thread.join(timeout=2)

            if self._stop_requested:
                self.status_changed.emit("Stopped")
                if (
                    Path(self.output_path).exists()
                    and Path(self.output_path).stat().st_size > 0
                ):
                    self.finished_ok.emit(self.output_path)
                else:
                    self.error_occurred.emit(
                        "Recording stopped but no output file was written. "
                        "The stream may not have been reachable."
                    )
            elif self._process.returncode != 0:
                err = self._stderr_tail.decode(errors="replace")
                self.error_occurred.emit(
                    f"FFmpeg error (code {self._process.returncode}):\n{err[-300:]}"
                )
            else:
                self.status_changed.emit("Finished")
                self.finished_ok.emit(self.output_path)

        except FileNotFoundError:
            self.error_occurred.emit(
                "FFmpeg not found — install it:\n"
                "  macOS: brew install ffmpeg\n"
                "  Linux: sudo apt install ffmpeg"
            )
        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            self.finished.emit()

    # ------------------------------------------------------------------
    # Stop — send 'q' to FFmpeg's stdin for a clean, muxer-finalised file
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """
        Ask FFmpeg to stop gracefully by writing 'q' to its stdin.

        FFmpeg intercepts the 'q' keystroke in its event loop and calls
        av_write_trailer() before exiting, which finalises the MP4 container
        (writes the moov atom / index).  Without this step the file is not
        seekable and most players will refuse to open it.

        We then give the process up to 8 seconds to finish muxing; if it
        still hasn't exited (e.g. the stdin pipe was closed by the OS before
        we wrote to it) we fall back to SIGTERM, and as a last resort SIGKILL.
        The drain thread keeps running throughout so the pipe never stalls.
        """
        self._stop_requested = True
        proc = self._process
        if proc is None or proc.poll() is not None:
            return  # already gone

        # 1. Polite: write 'q' + newline so FFmpeg flushes and exits cleanly.
        try:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
        except OSError:
            pass  # stdin may already be closed — fall through to terminate

        # 2. Wait up to 8 s for FFmpeg to write the MP4 trailer and exit.
        try:
            proc.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            pass

        # 3. Escalate to SIGTERM.
        try:
            proc.terminate()
            proc.wait(timeout=3)
            return
        except (subprocess.TimeoutExpired, OSError):
            pass

        # 4. Last resort: SIGKILL (file will likely be corrupt).
        try:
            proc.kill()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Snapshot worker
# ---------------------------------------------------------------------------

class SnapshotWorker(QThread):
    """
    Fetches a single JPEG still frame from ustreamer's /snapshot endpoint
    on a background thread and writes it to disk.

    ustreamer serves the current frame as a raw JPEG at GET /snapshot.
    We simply download the response body and write it to a .jpg file —
    no additional libraries required beyond the stdlib urllib.

    Signals
    -------
    finished_ok(path)  — emitted with the saved file path on success
    error_occurred(msg) — emitted with a human-readable error on failure
    """

    finished_ok    = Signal(str)
    error_occurred = Signal(str)

    def __init__(self, snapshot_url: str, output_path: str, parent=None) -> None:
        super().__init__(parent)
        self._url         = snapshot_url
        self._output_path = output_path

    def run(self) -> None:
        try:
            req = Request(self._url, headers={"User-Agent": "ROV-Viewer/1.0"})
            with urlopen(req, timeout=5) as resp:
                data = resp.read()

            if not data:
                self.error_occurred.emit("Snapshot: empty response from camera")
                return

            # Validate it looks like a JPEG (starts with FF D8)
            if not data[:2] == b"\xff\xd8":
                self.error_occurred.emit(
                    f"Snapshot: unexpected content type — "
                    f"first bytes: {data[:4].hex()}"
                )
                return

            os.makedirs(os.path.dirname(self._output_path), exist_ok=True)
            Path(self._output_path).write_bytes(data)
            self.finished_ok.emit(self._output_path)

        except URLError as exc:
            self.error_occurred.emit(f"Snapshot failed: {exc.reason}")
        except OSError as exc:
            self.error_occurred.emit(f"Snapshot save error: {exc}")
        except Exception as exc:
            self.error_occurred.emit(f"Snapshot error: {exc}")


# ---------------------------------------------------------------------------
# Combined camera panel (viewer + recorder)
# ---------------------------------------------------------------------------

class CameraPanel(QWidget):
    """
    Self-contained panel for one camera.
    Top half: live mpv viewer with connect/disconnect.
    Bottom half: FFmpeg recorder with record/stop and elapsed timer,
                 plus a snapshot button that grabs a still via ustreamer.
    """

    def __init__(self, title: str, prefix: str, parent=None) -> None:
        super().__init__(parent)
        self._title         = title
        self._prefix        = prefix
        self._current_url   = ""
        self._stream_active = False
        self._worker        = None
        self._snap_worker   = None   # active SnapshotWorker (if any)
        self._elapsed       = 0
        self._save_dir      = DEFAULT_SAVE
        self._last_save_path = ""

        # ==== VIEWER SECTION =============================================

        self._video = MpvWidget(self)

        self._stream_status = QLabel("Disconnected")
        self._stream_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._stream_status.setStyleSheet("color: #888; font-size: 12px; padding: 2px;")

        self._connect_btn    = QPushButton("Connect")
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)
        self._connect_btn.clicked.connect(self._on_connect_clicked)
        self._disconnect_btn.clicked.connect(self.disconnect_stream)

        viewer_btn_row = QHBoxLayout()
        viewer_btn_row.addWidget(self._connect_btn)
        viewer_btn_row.addWidget(self._disconnect_btn)

        # ==== DIVIDER ====================================================

        divider = QFrame()
        divider.setFrameStyle(QFrame.Shape.HLine | QFrame.Shadow.Sunken)

        # ==== RECORDER SECTION ===========================================

        self._rec_status = QLabel("Idle")
        self._rec_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._rec_status.setStyleSheet("color: #888; font-size: 11px;")

        self._duration_lbl = QLabel("")
        self._duration_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._duration_lbl.setStyleSheet("color: #aaa; font-size: 11px;")

        self._file_lbl = QLabel("")
        self._file_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._file_lbl.setStyleSheet("color: #777; font-size: 10px;")
        self._file_lbl.setWordWrap(True)

        # "Open file" button — hidden until a recording/snapshot is saved
        self._open_btn = QPushButton("▶  Open File")
        self._open_btn.setStyleSheet(
            "QPushButton { background-color: #1565c0; color: white; "
            "border-radius: 4px; padding: 3px 8px; font-size: 11px; }"
            "QPushButton:hover { background-color: #1e88e5; }"
        )
        self._open_btn.setVisible(False)
        self._open_btn.clicked.connect(self._open_saved_file)

        self._record_btn = QPushButton("● Record")
        self._record_btn.setStyleSheet(
            "QPushButton { background-color: #b71c1c; color: white; "
            "border-radius: 4px; padding: 3px 8px; font-weight: bold; }"
            "QPushButton:hover { background-color: #ef5350; }"
            "QPushButton:disabled { background-color: #444; color: #666; }"
        )
        self._stop_rec_btn = QPushButton("■ Stop")
        self._stop_rec_btn.setEnabled(False)
        self._stop_rec_btn.setStyleSheet(
            "QPushButton { background-color: #333; color: white; "
            "border-radius: 4px; padding: 3px 8px; }"
            "QPushButton:hover { background-color: #555; }"
            "QPushButton:disabled { background-color: #2a2a2a; color: #555; }"
        )

        # ---- Snapshot button --------------------------------------------
        self._snap_btn = QPushButton("📷")
        self._snap_btn.setToolTip("Capture a still frame (ustreamer /snapshot)")
        self._snap_btn.setFixedWidth(36)
        self._snap_btn.setStyleSheet(
            "QPushButton { background-color: #37474f; color: white; "
            "border-radius: 4px; padding: 3px 4px; font-size: 14px; }"
            "QPushButton:hover { background-color: #546e7a; }"
            "QPushButton:disabled { background-color: #2a2a2a; color: #555; }"
        )
        self._snap_btn.clicked.connect(self.take_snapshot)

        self._record_btn.clicked.connect(self.start_recording_from_ui)
        self._stop_rec_btn.clicked.connect(self.stop_recording)

        rec_btn_row = QHBoxLayout()
        rec_btn_row.addWidget(self._record_btn)
        rec_btn_row.addWidget(self._stop_rec_btn)
        rec_btn_row.addWidget(self._snap_btn)

        self._rec_timer = QTimer(self)
        self._rec_timer.setInterval(1000)
        self._rec_timer.timeout.connect(self._tick)

        # ==== TITLE ======================================================

        title_lbl = QLabel(f"<b>{title}</b>")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet("font-size: 13px; padding-bottom: 2px;")

        # ==== LAYOUT =====================================================

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(title_lbl)
        layout.addWidget(self._video, stretch=1)
        layout.addWidget(self._stream_status)
        layout.addLayout(viewer_btn_row)
        layout.addWidget(divider)
        layout.addWidget(self._rec_status)
        layout.addWidget(self._duration_lbl)
        layout.addWidget(self._file_lbl)
        layout.addWidget(self._open_btn)
        layout.addLayout(rec_btn_row)

        self.setStyleSheet(
            "CameraPanel { border: 1px solid #555; border-radius: 6px; }"
        )

        # Reconnect heartbeat
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(2000)
        self._heartbeat.timeout.connect(self._check_connection)

    # ------------------------------------------------------------------
    # Viewer public API
    # ------------------------------------------------------------------

    def set_url(self, url: str) -> None:
        self._current_url = url

    def connect_stream(self, url: str) -> None:
        if not url:
            self._set_stream_status("No URL configured", error=True)
            return
        self._current_url   = url
        self._stream_active = True
        self._video.play(url)
        self._heartbeat.start()
        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._set_stream_status(f"Connecting → {url}")

        @self._video.player.property_observer("idle-active")
        def _on_idle(name, idle):   # noqa: ARG001
            if not self._stream_active:
                return
            if idle:
                self._set_stream_status("⚠  Stream lost — reconnecting…", error=True)
            else:
                self._set_stream_status(f"● Live: {self._current_url}", live=True)

    def disconnect_stream(self) -> None:
        self._stream_active = False
        self._heartbeat.stop()
        self._video.stop()
        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._set_stream_status("Disconnected")

    def is_stream_active(self) -> bool:
        return self._stream_active

    # ------------------------------------------------------------------
    # Recorder public API
    # ------------------------------------------------------------------

    def set_save_dir(self, path: str) -> None:
        self._save_dir = path

    def start_recording(self, url: str, save_dir: str) -> bool:
        if self.is_recording():
            return True
        if not url:
            self._set_rec_status("No URL", error=True)
            return False

        # Ensure the save directory actually exists before we start
        try:
            os.makedirs(save_dir, exist_ok=True)
        except OSError as e:
            self._set_rec_status("Save dir error", error=True)
            self._file_lbl.setText(str(e))
            return False

        timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(save_dir, f"{self._prefix}_{timestamp}.mp4")

        self._last_save_path = output_path
        self._open_btn.setVisible(False)  # hide previous open button

        self._worker = RecordingWorker(url, output_path)
        self._worker.status_changed.connect(self._on_rec_status)
        self._worker.error_occurred.connect(self._on_rec_error)
        self._worker.finished_ok.connect(self._on_rec_finished_ok)
        self._worker.finished.connect(self._on_rec_finished)
        self._worker.start()

        self._elapsed = 0
        self._rec_timer.start()
        self._file_lbl.setText(Path(output_path).name)
        self._duration_lbl.setText("00:00")
        self._record_btn.setEnabled(False)
        self._stop_rec_btn.setEnabled(True)
        self._set_rec_status("Starting…")
        return True

    def start_recording_from_ui(self) -> None:
        self.start_recording(self._current_url, self._save_dir)

    def stop_recording(self) -> None:
        if self._worker:
            self._worker.stop()
        self._rec_timer.stop()
        self._stop_rec_btn.setEnabled(False)

    def is_recording(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    # ------------------------------------------------------------------
    # Snapshot public API
    # ------------------------------------------------------------------

    def take_snapshot(self, snapshot_url: str = "", save_dir: str = "") -> None:
        """
        Fetch a still JPEG from ustreamer's /snapshot endpoint.

        Parameters
        ----------
        snapshot_url : str
            Full URL of the snapshot endpoint.  If empty, it is derived from
            ``self._current_url`` by replacing the path with ``/snapshot``.
        save_dir : str
            Directory to save the JPEG.  Falls back to ``self._save_dir``.

        The download runs on a ``SnapshotWorker`` QThread so the UI is never
        blocked.  Only one snapshot per panel can be in-flight at a time;
        subsequent clicks are ignored until the worker finishes.
        """
        if self._snap_worker and self._snap_worker.isRunning():
            return  # already fetching — ignore the extra click

        # Build the snapshot URL from the stream URL when not given explicitly.
        if not snapshot_url:
            if not self._current_url:
                self._set_rec_status("No URL — connect first", error=True)
                return
            # Replace just the path component: http://ip:port/stream → /snapshot
            from urllib.parse import urlparse, urlunparse
            parts = urlparse(self._current_url)
            snapshot_url = urlunparse(parts._replace(path=SNAPSHOT_PATH))

        target_dir = save_dir or self._save_dir
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as exc:
            self._set_rec_status("Save dir error", error=True)
            self._file_lbl.setText(str(exc))
            return

        timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(target_dir, f"{self._prefix}_snap_{timestamp}.jpg")

        self._snap_btn.setEnabled(False)
        self._set_rec_status("📷 Capturing…")
        self._open_btn.setVisible(False)

        self._snap_worker = SnapshotWorker(snapshot_url, output_path)
        self._snap_worker.finished_ok.connect(self._on_snap_ok)
        self._snap_worker.error_occurred.connect(self._on_snap_error)
        # Re-enable the button once the thread finishes (success or failure)
        self._snap_worker.finished.connect(lambda: self._snap_btn.setEnabled(True))
        self._snap_worker.start()

    # ------------------------------------------------------------------
    # Internal — viewer
    # ------------------------------------------------------------------

    def _on_connect_clicked(self) -> None:
        self.connect_stream(self._current_url)

    def _set_stream_status(self, msg: str, *, live=False, error=False) -> None:
        color = "#4caf50" if live else ("#e57373" if error else "#888")
        self._stream_status.setStyleSheet(
            f"color: {color}; font-size: 12px; padding: 2px;"
        )
        self._stream_status.setText(msg)

    def _check_connection(self) -> None:
        if not self._stream_active:
            return
        try:
            idle = self._video.player.idle_active
        except Exception:
            idle = True
        if idle:
            self._video.play(self._current_url)

    # ------------------------------------------------------------------
    # Internal — recorder
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        self._elapsed += 1
        m, s = divmod(self._elapsed, 60)
        h, m = divmod(m, 60)
        if h:
            self._duration_lbl.setText(f"{h:02d}:{m:02d}:{s:02d}")
        else:
            self._duration_lbl.setText(f"{m:02d}:{s:02d}")

    def _on_rec_status(self, msg: str) -> None:
        self._set_rec_status(msg, live="Recording" in msg)

    def _on_rec_error(self, msg: str) -> None:
        self._set_rec_status("Error", error=True)
        self._file_lbl.setText(msg[:80])
        self._rec_timer.stop()

    def _on_rec_finished_ok(self, path: str) -> None:
        """Called when FFmpeg exits cleanly (including after a user Stop)."""
        self._last_save_path = path
        self._set_rec_status("Saved ✓", live=True)
        self._file_lbl.setText(Path(path).name)
        self._open_btn.setVisible(True)

    def _on_rec_finished(self) -> None:
        """Always called at end of recording, regardless of outcome."""
        self._record_btn.setEnabled(True)
        self._stop_rec_btn.setEnabled(False)
        self._rec_timer.stop()

    def _set_rec_status(self, msg: str, *, live=False, error=False) -> None:
        color = "#4caf50" if live else ("#ef5350" if error else "#888")
        self._rec_status.setStyleSheet(f"color: {color}; font-size: 11px;")
        self._rec_status.setText(msg)

    def _open_saved_file(self) -> None:
        """Open the saved video/image file in the system default application."""
        if self._last_save_path and Path(self._last_save_path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._last_save_path))
        else:
            self._file_lbl.setText("File not found — may still be writing")

    # ------------------------------------------------------------------
    # Internal — snapshot callbacks
    # ------------------------------------------------------------------

    def _on_snap_ok(self, path: str) -> None:
        """Called on the Qt main thread when the JPEG has been written."""
        self._last_save_path = path
        self._set_rec_status("📷 Snapshot saved ✓", live=True)
        self._file_lbl.setText(Path(path).name)
        self._open_btn.setVisible(True)

    def _on_snap_error(self, msg: str) -> None:
        self._set_rec_status("Snapshot failed", error=True)
        self._file_lbl.setText(msg[:80])

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        self._stream_active = False
        self._heartbeat.stop()
        self.stop_recording()
        self._video.shutdown()

    def closeEvent(self, event) -> None:
        self.shutdown()
        event.accept()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ROVDualViewer(QMainWindow):
    """
    Combined viewer + recorder. Two CameraPanel widgets side by side.
    Shared controls bar for IP, ports, save directory, and bulk actions.
    """

    def __init__(self, ip: str, port1: int, port2: int) -> None:
        super().__init__()
        self.setWindowTitle("ROV Dual Camera Viewer + Recorder")

        # ---- Camera panels ----------------------------------------------
        self._cam1 = CameraPanel("Camera 1", "cam1")
        self._cam2 = CameraPanel("Camera 2", "cam2")

        cam_row = QHBoxLayout()
        cam_row.setSpacing(8)
        cam_row.addWidget(self._cam1)
        cam_row.addWidget(self._cam2)

        # ---- Shared controls bar ----------------------------------------
        self._ip_edit    = QLineEdit(ip)
        self._port1_edit = QLineEdit(str(port1))
        self._port2_edit = QLineEdit(str(port2))
        self._save_edit  = QLineEdit(DEFAULT_SAVE)

        for widget, placeholder in (
            (self._ip_edit,    "192.168.x.x"),
            (self._port1_edit, "8080"),
            (self._port2_edit, "8081"),
        ):
            widget.setMaximumWidth(120)
            widget.setPlaceholderText(placeholder)

        self._save_edit.setMinimumWidth(180)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(75)
        browse_btn.clicked.connect(self._browse_save_dir)

        # Sync save dir into panels whenever the field changes
        self._save_edit.textChanged.connect(self._sync_save_dir)

        for field in (self._ip_edit, self._port1_edit, self._port2_edit):
            field.returnPressed.connect(self._on_connect_all)

        # Stream buttons
        self._connect_all_btn    = QPushButton("▶  Connect Both")
        self._disconnect_all_btn = QPushButton("■  Disconnect Both")
        self._disconnect_all_btn.setEnabled(False)
        self._connect_all_btn.clicked.connect(self._on_connect_all)
        self._disconnect_all_btn.clicked.connect(self._on_disconnect_all)

        # Record buttons
        self._record_all_btn = QPushButton("●  Record Both")
        self._record_all_btn.setStyleSheet(
            "QPushButton { background-color: #b71c1c; color: white; "
            "font-weight: bold; border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background-color: #e53935; }"
            "QPushButton:disabled { background-color: #444; color: #666; }"
        )
        self._stop_all_btn = QPushButton("■  Stop Both")
        self._stop_all_btn.setEnabled(False)
        self._stop_all_btn.setStyleSheet(
            "QPushButton { background-color: #212121; color: white; "
            "font-weight: bold; border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background-color: #424242; }"
            "QPushButton:disabled { background-color: #333; color: #555; }"
        )
        self._record_all_btn.clicked.connect(self._on_record_all)
        self._stop_all_btn.clicked.connect(self._on_stop_all)

        # ---- Snapshot Both button ---------------------------------------
        self._snap_all_btn = QPushButton("📷  Snapshot Both")
        self._snap_all_btn.setToolTip(
            "Capture a still frame from both cameras simultaneously"
        )
        self._snap_all_btn.setStyleSheet(
            "QPushButton { background-color: #37474f; color: white; "
            "font-weight: bold; border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background-color: #546e7a; }"
        )
        self._snap_all_btn.clicked.connect(self._on_snapshot_all)

        # Controls grid
        ctrl = QGridLayout()
        ctrl.setSpacing(6)
        ctrl.addWidget(QLabel("ROV IP:"),      0, 0, Qt.AlignmentFlag.AlignRight)
        ctrl.addWidget(self._ip_edit,          0, 1)
        ctrl.addWidget(QLabel("Cam 1 port:"),  0, 2, Qt.AlignmentFlag.AlignRight)
        ctrl.addWidget(self._port1_edit,       0, 3)
        ctrl.addWidget(QLabel("Cam 2 port:"),  0, 4, Qt.AlignmentFlag.AlignRight)
        ctrl.addWidget(self._port2_edit,       0, 5)
        ctrl.addWidget(QLabel("Save to:"),     1, 0, Qt.AlignmentFlag.AlignRight)
        ctrl.addWidget(self._save_edit,        1, 1, 1, 4)
        ctrl.addWidget(browse_btn,             1, 5)

        stream_row = QHBoxLayout()
        stream_row.addWidget(self._connect_all_btn)
        stream_row.addWidget(self._disconnect_all_btn)
        stream_row.addSpacing(24)
        stream_row.addWidget(self._record_all_btn)
        stream_row.addWidget(self._stop_all_btn)
        stream_row.addSpacing(24)
        stream_row.addWidget(self._snap_all_btn)
        stream_row.addStretch()

        # ---- Root layout ------------------------------------------------
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        root.addLayout(cam_row, stretch=1)

        sep = QFrame()
        sep.setFrameStyle(QFrame.Shape.HLine | QFrame.Shadow.Sunken)
        root.addWidget(sep)

        root.addLayout(ctrl)
        root.addLayout(stream_row)
        self.setCentralWidget(central)

        # Initialise panels with the default save dir
        self._sync_save_dir(DEFAULT_SAVE)

        # ---- Status bar -------------------------------------------------
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage(
            f"Ready — recordings will be saved to: {DEFAULT_SAVE}"
        )

        self.resize(1280, 700)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_urls(self) -> tuple[str, str]:
        ip    = self._ip_edit.text().strip()
        port1 = self._port1_edit.text().strip()
        port2 = self._port2_edit.text().strip()
        return (
            f"http://{ip}:{port1}{STREAM_PATH}",
            f"http://{ip}:{port2}{STREAM_PATH}",
        )

    def _build_snapshot_urls(self) -> tuple[str, str]:
        ip    = self._ip_edit.text().strip()
        port1 = self._port1_edit.text().strip()
        port2 = self._port2_edit.text().strip()
        return (
            f"http://{ip}:{port1}{SNAPSHOT_PATH}",
            f"http://{ip}:{port2}{SNAPSHOT_PATH}",
        )

    def _browse_save_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Save Directory", self._save_edit.text()
        )
        if path:
            self._save_edit.setText(path)

    def _sync_save_dir(self, path: str) -> None:
        """Push the current save-dir text into both camera panels."""
        self._cam1.set_save_dir(path)
        self._cam2.set_save_dir(path)

    # ------------------------------------------------------------------
    # Slots — streaming
    # ------------------------------------------------------------------

    def _on_connect_all(self) -> None:
        url1, url2 = self._build_urls()
        self._cam1.connect_stream(url1)
        self._cam2.connect_stream(url2)
        self._connect_all_btn.setEnabled(False)
        self._disconnect_all_btn.setEnabled(True)
        self._status_bar.showMessage(
            f"Connecting  |  Cam1 → {url1}  |  Cam2 → {url2}"
        )

    def _on_disconnect_all(self) -> None:
        self._cam1.disconnect_stream()
        self._cam2.disconnect_stream()
        self._connect_all_btn.setEnabled(True)
        self._disconnect_all_btn.setEnabled(False)
        self._status_bar.showMessage("Both cameras disconnected")

    # ------------------------------------------------------------------
    # Slots — recording
    # ------------------------------------------------------------------

    def _on_record_all(self) -> None:
        url1, url2 = self._build_urls()
        save_dir   = self._save_edit.text().strip() or DEFAULT_SAVE
        ok1 = self._cam1.start_recording(url1, save_dir)
        ok2 = self._cam2.start_recording(url2, save_dir)
        if ok1 or ok2:
            self._record_all_btn.setEnabled(False)
            self._stop_all_btn.setEnabled(True)
            self._status_bar.showMessage(
                f"Recording both cameras → saving to {save_dir}"
            )

    def _on_stop_all(self) -> None:
        self._cam1.stop_recording()
        self._cam2.stop_recording()
        self._record_all_btn.setEnabled(True)
        self._stop_all_btn.setEnabled(False)
        self._status_bar.showMessage(
            "Recording stopped — files saved to " + self._save_edit.text()
        )

    # ------------------------------------------------------------------
    # Slots — snapshot
    # ------------------------------------------------------------------

    def _on_snapshot_all(self) -> None:
        """Trigger a simultaneous snapshot on both camera panels."""
        snap1, snap2 = self._build_snapshot_urls()
        save_dir = self._save_edit.text().strip() or DEFAULT_SAVE
        self._cam1.take_snapshot(snapshot_url=snap1, save_dir=save_dir)
        self._cam2.take_snapshot(snapshot_url=snap2, save_dir=save_dir)
        self._status_bar.showMessage(
            f"📷 Capturing snapshots from both cameras → {save_dir}"
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._cam1.shutdown()
        self._cam2.shutdown()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ROV Dual Camera Viewer + Recorder")
    parser.add_argument("--ip",    default=DEFAULT_IP,
                        help="ROV IP address (default: %(default)s)")
    parser.add_argument("--port1", type=int, default=DEFAULT_PORT1,
                        help="Camera 1 port (default: %(default)s)")
    parser.add_argument("--port2", type=int, default=DEFAULT_PORT2,
                        help="Camera 2 port (default: %(default)s)")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("ROV Dual Viewer + Recorder")

    window = ROVDualViewer(ip=args.ip, port1=args.port1, port2=args.port2)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()