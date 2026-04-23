"""
rov_recorder.py
---------------
ROV Single Camera Recorder

Records one camera stream at a time to an MP4 file using FFmpeg.
The user picks which camera to record via a dropdown, then hits Record.

Layout:
    ┌──────────────────────────────────────────┐
    │  Camera: [Camera 1 ▼]                    │
    │  ROV IP: [_______]  Port: [____]         │
    │  Save to: [___________________________]  │
    │                                          │
    │  Status: Idle                            │
    │  Duration: —                             │
    │  File: —                                 │
    │                                          │
    │       [● Start Recording]                │
    │       [■ Stop Recording]                 │
    └──────────────────────────────────────────┘

Requirements:
    pip install PyQt6
    FFmpeg must be on PATH:
        macOS:  brew install ffmpeg
        Linux:  sudo apt install ffmpeg
        Windows: https://ffmpeg.org/download.html

Usage:
    python rov_recorder.py
    python rov_recorder.py --ip 192.168.1.50 --port1 8080 --port2 8081
"""

import sys
import os
import argparse
import subprocess
from datetime import datetime
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QPushButton, QLineEdit, QLabel,
    QStatusBar, QFileDialog, QComboBox, QFrame,
)
from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt6.QtGui import QFont


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_IP    = "192.168.1.100"
DEFAULT_PORT1 = 8080
DEFAULT_PORT2 = 8081
STREAM_PATH   = "/stream"
DEFAULT_SAVE  = str(Path.home() / "Videos")


# ---------------------------------------------------------------------------
# FFmpeg recording worker
# ---------------------------------------------------------------------------

class RecordingWorker(QThread):
    """Runs FFmpeg in a background thread to record an MJPEG stream to MP4."""

    status_changed = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    finished       = pyqtSignal()

    def __init__(self, url: str, output_path: str, parent=None) -> None:
        super().__init__(parent)
        self.url         = url
        self.output_path = output_path
        self._process: subprocess.Popen | None = None
        self._stop_requested = False

    def run(self) -> None:
        cmd = [
            "ffmpeg",
            "-y",
            "-fflags", "nobuffer",
            "-flags",  "low_delay",
            "-i",      self.url,
            "-c:v",    "libx264",
            "-preset", "ultrafast",
            "-crf",    "23",
            "-movflags", "+faststart",
            "-an",
            self.output_path,
        ]
        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.status_changed.emit(f"Recording → {Path(self.output_path).name}")
            self._process.wait()

            if self._stop_requested:
                self.status_changed.emit("Stopped")
            elif self._process.returncode != 0:
                err = self._process.stderr.read().decode(errors="replace")
                self.error_occurred.emit(
                    f"FFmpeg exited with code {self._process.returncode}.\n{err[-400:]}"
                )
            else:
                self.status_changed.emit("Finished")

        except FileNotFoundError:
            self.error_occurred.emit(
                "FFmpeg not found.\n"
                "  macOS:  brew install ffmpeg\n"
                "  Linux:  sudo apt install ffmpeg"
            )
        except Exception as exc:
            self.error_occurred.emit(str(exc))
        finally:
            self.finished.emit()

    def stop(self) -> None:
        self._stop_requested = True
        if self._process and self._process.poll() is None:
            try:
                self._process.stdin.write(b"q\n")
                self._process.stdin.flush()
            except Exception:
                pass
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ROVRecorder(QMainWindow):

    def __init__(self, ip: str, port1: int, port2: int) -> None:
        super().__init__()
        self.setWindowTitle("ROV Camera Recorder")
        self._port1   = port1
        self._port2   = port2
        self._worker: RecordingWorker | None = None
        self._elapsed = 0

        # ---- Camera selector --------------------------------------------
        self._camera_combo = QComboBox()
        self._camera_combo.addItem("Camera 1", userData=port1)
        self._camera_combo.addItem("Camera 2", userData=port2)
        self._camera_combo.currentIndexChanged.connect(self._update_port_field)

        # ---- Connection fields ------------------------------------------
        self._ip_edit   = QLineEdit(ip)
        self._port_edit = QLineEdit(str(port1))
        self._port_edit.setMaximumWidth(80)

        # ---- Save directory ---------------------------------------------
        self._save_edit = QLineEdit(DEFAULT_SAVE)
        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(80)
        browse_btn.clicked.connect(self._browse)

        save_row = QHBoxLayout()
        save_row.addWidget(self._save_edit)
        save_row.addWidget(browse_btn)

        # ---- Form layout ------------------------------------------------
        form = QFormLayout()
        form.setSpacing(8)
        form.addRow("Camera:", self._camera_combo)
        form.addRow("ROV IP:", self._ip_edit)
        form.addRow("Port:", self._port_edit)
        form.addRow("Save to:", save_row)

        # ---- Info panel -------------------------------------------------
        separator = QFrame()
        separator.setFrameStyle(QFrame.Shape.HLine | QFrame.Shadow.Sunken)

        self._status_lbl = QLabel("Idle")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_lbl.setStyleSheet("font-size: 13px; color: #888;")

        self._duration_lbl = QLabel("Duration: —")
        self._duration_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._duration_lbl.setStyleSheet("font-size: 12px;")

        self._file_lbl = QLabel("File: —")
        self._file_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._file_lbl.setStyleSheet("font-size: 11px; color: #aaa;")
        self._file_lbl.setWordWrap(True)

        # ---- Buttons ----------------------------------------------------
        self._record_btn = QPushButton("● Start Recording")
        self._record_btn.setFixedHeight(40)
        self._record_btn.setFont(QFont("sans-serif", 12, QFont.Weight.Bold))
        self._record_btn.setStyleSheet(
            "QPushButton { background-color: #c62828; color: white; border-radius: 6px; }"
            "QPushButton:hover { background-color: #ef5350; }"
            "QPushButton:disabled { background-color: #444; color: #777; }"
        )

        self._stop_btn = QPushButton("■ Stop Recording")
        self._stop_btn.setFixedHeight(40)
        self._stop_btn.setFont(QFont("sans-serif", 12, QFont.Weight.Bold))
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            "QPushButton { background-color: #333; color: white; border-radius: 6px; }"
            "QPushButton:hover { background-color: #555; }"
            "QPushButton:disabled { background-color: #2a2a2a; color: #555; }"
        )

        self._record_btn.clicked.connect(self._on_record)
        self._stop_btn.clicked.connect(self._on_stop)

        # ---- Elapsed timer ----------------------------------------------
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

        # ---- Root layout ------------------------------------------------
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)
        root.addLayout(form)
        root.addWidget(separator)
        root.addWidget(self._status_lbl)
        root.addWidget(self._duration_lbl)
        root.addWidget(self._file_lbl)
        root.addSpacing(4)
        root.addWidget(self._record_btn)
        root.addWidget(self._stop_btn)
        self.setCentralWidget(central)

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")

        self.setFixedWidth(420)
        self.adjustSize()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_url(self) -> str:
        ip   = self._ip_edit.text().strip()
        port = self._port_edit.text().strip()
        return f"http://{ip}:{port}{STREAM_PATH}"

    def _update_port_field(self) -> None:
        port = self._camera_combo.currentData()
        self._port_edit.setText(str(port))

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Save Directory", self._save_edit.text()
        )
        if path:
            self._save_edit.setText(path)

    def _set_status(self, msg: str, *, live=False, error=False) -> None:
        color = "#4caf50" if live else ("#ef5350" if error else "#888")
        self._status_lbl.setStyleSheet(f"font-size: 13px; color: {color};")
        self._status_lbl.setText(msg)

    def _tick(self) -> None:
        self._elapsed += 1
        m, s = divmod(self._elapsed, 60)
        h, m = divmod(m, 60)
        if h:
            self._duration_lbl.setText(f"Duration: {h:02d}:{m:02d}:{s:02d}")
        else:
            self._duration_lbl.setText(f"Duration: {m:02d}:{s:02d}")

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_record(self) -> None:
        url      = self._build_url()
        save_dir = self._save_edit.text().strip() or DEFAULT_SAVE
        cam_name = self._camera_combo.currentText().lower().replace(" ", "")

        os.makedirs(save_dir, exist_ok=True)
        timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(save_dir, f"{cam_name}_{timestamp}.mp4")

        self._worker = RecordingWorker(url, output_path)
        self._worker.status_changed.connect(self._on_worker_status)
        self._worker.error_occurred.connect(self._on_worker_error)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()

        self._elapsed = 0
        self._timer.start()
        self._file_lbl.setText(f"File: {Path(output_path).name}")
        self._record_btn.setEnabled(False)
        self._camera_combo.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._set_status("Connecting…")
        self._status_bar.showMessage(
            f"Recording {self._camera_combo.currentText()} → {url}"
        )

    def _on_stop(self) -> None:
        if self._worker:
            self._worker.stop()
        self._timer.stop()
        self._stop_btn.setEnabled(False)

    def _on_worker_status(self, msg: str) -> None:
        self._set_status(msg, live="Recording" in msg)

    def _on_worker_error(self, msg: str) -> None:
        self._set_status("Error — see status bar", error=True)
        self._status_bar.showMessage(f"Error: {msg[:120]}")
        self._timer.stop()

    def _on_worker_finished(self) -> None:
        self._record_btn.setEnabled(True)
        self._camera_combo.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._timer.stop()
        if "Error" not in self._status_lbl.text():
            self._set_status("Saved ✓", live=True)
            self._status_bar.showMessage(
                "Saved: " + self._file_lbl.text().replace("File: ", "")
            )

    def closeEvent(self, event) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ROV Camera Recorder")
    parser.add_argument("--ip",    default=DEFAULT_IP)
    parser.add_argument("--port1", type=int, default=DEFAULT_PORT1)
    parser.add_argument("--port2", type=int, default=DEFAULT_PORT2)
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("ROV Recorder")

    window = ROVRecorder(ip=args.ip, port1=args.port1, port2=args.port2)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
