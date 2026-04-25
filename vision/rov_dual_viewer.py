"""
rov_dual_viewer.py
------------------
ROV Camera Viewer — dual camera edition.

Both cameras share the same IP address but stream on different ports.
Each camera panel has its own:
  • MpvWidget  (independent GPU-accelerated OpenGL renderer)
  • Status label
  • Connect / Disconnect buttons
  • 2-second auto-reconnect heartbeat

Layout:
    ┌──────────────────────────────────────────────────┐
    │  ┌───── Camera 1 ──────┐  ┌───── Camera 2 ─────┐ │
    │  │                     │  │                    │ │
    │  │    MpvWidget        │  │    MpvWidget       │ │
    │  │                     │  │                    │ │
    │  │  status: ● Live     │  │  status: ● Live    │ │
    │  │  [Connect][Disconn] │  │  [Connect][Disconn]│ │
    │  └─────────────────────┘  └────────────────────┘ │
    │  ROV IP: [_______]  Cam1: [____]  Cam2: [______] │
    │          [▶ Connect Both]  [■ Disconnect Both]   │
    └──────────────────────────────────────────────────┘

Requirements:
    pip install python-mpv PySide6
    macOS:  brew install mpv
    Linux:  sudo apt install libmpv-dev

Usage:
    python rov_dual_viewer.py
    python rov_dual_viewer.py --ip 192.168.1.50 --port1 8080 --port2 8081
"""

import sys
import os
import ctypes
import ctypes.util
import locale
import argparse

# Fix libmpv float parsing before the library loads (some languages use "," instead of "." as decimal point, lib_mpv required American styled)
# os.environ["LC_NUMERIC"] = "C"
# _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
# _libc.setlocale.restype = ctypes.c_char_p
# _libc.setlocale(locale.LC_NUMERIC, b"C")

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout,
    QPushButton, QLineEdit, QLabel, QStatusBar,
)
from PySide6.QtCore import Qt, QTimer

# Import the MpvWidget from your existing rov_viewer.py (same directory).
from mpv_viewer import MpvWidget

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_IP    = "192.168.1.2"
DEFAULT_PORT1 = 8081
DEFAULT_PORT2 = 5051
STREAM_PATH   = "/stream"   # change to e.g. "/?action=stream" if needed


# ---------------------------------------------------------------------------
# Single-camera panel
# ---------------------------------------------------------------------------

class CameraPanel(QWidget):
    """
    Self-contained panel for one camera stream.

    Owns one MpvWidget, a status label, per-panel connect/disconnect
    buttons, and a 2-second auto-reconnect heartbeat.
    """

    def __init__(self, title: str, parent=None) -> None:
        super().__init__(parent) ##calls attributes of parent class QWidget for this class CameraPanel to inherit from it.
        self._title         = title
        self._current_url   = ""
        self._stream_active = False

        # ---- Video -------------------------------------------------------
        self._video = MpvWidget(self)

        # ---- Status label ------------------------------------------------
        self._status = QLabel("Disconnected")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)  #This line sets the alignment of the text within the QLabel widget to be centered both horizontally and vertically. 
        self._status.setStyleSheet("color: #888; font-size: 12px; padding: 2px;")

        # ---- Per-panel buttons -------------------------------------------
        self._connect_btn    = QPushButton("Connect")
        self._disconnect_btn = QPushButton("Disconnect")
        self._disconnect_btn.setEnabled(False)

        self._connect_btn.clicked.connect(self._on_connect_clicked)
        self._disconnect_btn.clicked.connect(self.disconnect)

        btn_row = QHBoxLayout() ##QHBoxLayout() is a layout manager that arranges widgets horizontally. When you create an instance of QHBoxLayout(), it starts as an empty layout with no widgets in it. You can then add widgets to this layout using the addWidget() method, and it will automatically arrange them in a horizontal row. In this code, btn_row is an instance of QHBoxLayout that will hold the Connect and Disconnect buttons side by side.
        btn_row.addWidget(self._connect_btn) 
        btn_row.addWidget(self._disconnect_btn)

        # ---- Title label -------------------------------------------------
        title_lbl = QLabel(f"<b>{title}</b>")
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_lbl.setStyleSheet("font-size: 13px; padding-bottom: 2px;")

        # ---- Layout ------------------------------------------------------
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addWidget(title_lbl)
        layout.addWidget(self._video, stretch=1)
        layout.addWidget(self._status)
        layout.addLayout(btn_row)

        # ---- Subtle border -----------------------------------------------
        self.setStyleSheet(
            "CameraPanel { border: 1px solid #555; border-radius: 6px; }"
        )

        # ---- Reconnect heartbeat ----------------------------------------
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(2000)
        self._heartbeat.timeout.connect(self._check_connection)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_url(self, url: str) -> None:
        """Called by the main window to push a new URL before connecting."""
        self._current_url = url

    def connect(self, url: str) -> None:
        """Start streaming *url*. Safe to call repeatedly (replaces old stream)."""
        if not url:
            self._set_status("No URL configured", error=True)
            return

        self._current_url   = url
        self._stream_active = True
        self._video.play(url)
        self._heartbeat.start()

        self._connect_btn.setEnabled(False)
        self._disconnect_btn.setEnabled(True)
        self._set_status(f"Connecting → {url}")

        # Property observer for live/lost feedback.
        @self._video.player.property_observer("idle-active")
        def _on_idle(name, idle):   # noqa: ARG001
            if not self._stream_active:
                return
            if idle:
                self._set_status("⚠  Stream lost — reconnecting…", error=True)
            else:
                self._set_status(f"● Live: {self._current_url}", live=True)

    def disconnect(self) -> None:
        """Stop streaming and reset UI."""
        self._stream_active = False
        self._heartbeat.stop()
        self._video.stop()

        self._connect_btn.setEnabled(True)
        self._disconnect_btn.setEnabled(False)
        self._set_status("Disconnected")

    def is_active(self) -> bool:
        return self._stream_active

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_connect_clicked(self) -> None:
        """Per-panel Connect button — reconnect using the last known URL."""
        self.connect(self._current_url)

    def _set_status(self, msg: str, *, live: bool = False, error: bool = False) -> None:
        color = "#4caf50" if live else ("#e57373" if error else "#888")
        self._status.setStyleSheet(
            f"color: {color}; font-size: 12px; padding: 2px;"
        )
        self._status.setText(msg)

    def _check_connection(self) -> None:
        """Heartbeat callback: replay if mpv went idle unexpectedly."""
        if not self._stream_active:
            return
        try:
            idle = self._video.player.idle_active
        except Exception:
            idle = True   # can't query → assume dropped

        if idle:
            self._video.play(self._current_url)

    # ------------------------------------------------------------------
    # Forward close event so MpvWidget can clean up safely
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._stream_active = False
        self._heartbeat.stop()
        # Delegate to MpvWidget's closeEvent which handles the safe
        # mpv shutdown sequence (stop → wait → free ctx → terminate).
        self._video.closeEvent(event)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ROVDualViewer(QMainWindow):
    """
    Main window with two CameraPanel widgets side-by-side.

    A shared controls bar holds the ROV IP and per-camera port fields.
    'Connect Both' / 'Disconnect Both' operate both streams at once;
    individual panel buttons allow independent control.
    """

    def __init__(self, ip: str, port1: int, port2: int) -> None:
        super().__init__()
        self.setWindowTitle("ROV Dual Camera Viewer")
        self._camera_mode = "Dual"  # changes to "Single Cam 1" or "Single Cam 2" if user disconnects one panel manually

        #-----This code below defines the dual interface. The toggling gets defined later in slots section.
        
        # ---- Camera panels ----------------------------------------------
        self._cam1 = CameraPanel("Camera 1")
        self._cam2 = CameraPanel("Camera 2")

        cam_row = QHBoxLayout()
        cam_row.setSpacing(8)
        cam_row.addWidget(self._cam1)
        cam_row.addWidget(self._cam2)

        # ---- Shared controls bar ----------------------------------------
        self._ip_edit    = QLineEdit(ip) #generic
        self._port1_edit = QLineEdit(str(port1)) #specific to cam1
        self._port2_edit = QLineEdit(str(port2)) #specific to cam2
        
        for widget, placeholder in (
            (self._ip_edit,    "192.168.x.x"),
            (self._port1_edit, "8080"),
            (self._port2_edit, "8081")
            
        ):
            widget.setMaximumWidth(130)
            widget.setPlaceholderText(placeholder)

        # Enter in any field triggers Connect Both
        for field in (self._ip_edit, self._port1_edit, self._port2_edit):
            field.returnPressed.connect(self._on_connect_all)

        self._connect_all_btn    = QPushButton("▶  Connect Both")
        self._disconnect_all_btn = QPushButton("■  Disconnect Both")
        self._disconnect_all_btn.setEnabled(False)

        self._connect_all_btn.clicked.connect(self._on_connect_all)
        self._disconnect_all_btn.clicked.connect(self._on_disconnect_all)
        

        ##----Misha edit: added toggle buttons for single cam view modes, not implemented yet
        self._toggle_view_cam1_btn = QPushButton("Toggle View - Cam 1")
        self._toggle_view_cam2_btn = QPushButton("Toggle View - Cam 2")
        self._toggle_view_dual_btn = QPushButton("Toggle View - Dual")

        self._toggle_view_cam1_btn.clicked.connect(self._toggle_view_cam1)
        self._toggle_view_cam2_btn.clicked.connect(self._toggle_view_cam2)
        self._toggle_view_dual_btn.clicked.connect(self._toggle_view_dual)
        ###---End misha edit

        controls = QHBoxLayout()
        controls.addWidget(QLabel("ROV IP:"))
        controls.addWidget(self._ip_edit)
        controls.addSpacing(16)
        controls.addWidget(QLabel("Cam 1 port:"))
        controls.addWidget(self._port1_edit)
        controls.addSpacing(16)
        controls.addWidget(QLabel("Cam 2 port:"))
        controls.addWidget(self._port2_edit)
        controls.addSpacing(24)
        controls.addWidget(self._connect_all_btn)
        controls.addWidget(self._disconnect_all_btn)
        controls.addStretch()

        toggle = QHBoxLayout()
        toggle.addWidget(self._toggle_view_cam1_btn)
        toggle.addSpacing(16)
        toggle.addWidget(self._toggle_view_cam2_btn)
        toggle.addSpacing(16)
        toggle.addWidget(self._toggle_view_dual_btn)
        toggle.addStretch()
        
        ##for start, hide dual button
        self._toggle_view_dual_btn.setVisible(False)
        

        # ---- Root layout ------------------------------------------------
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)
        root.addLayout(cam_row, stretch=1)
        root.addLayout(controls)
        root.addLayout(toggle)
        self.setCentralWidget(central)

        # ---- Status bar -------------------------------------------------
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage(
            "Ready — set IP and ports, then click ▶ Connect Both"
        )

        self.resize(1280, 600)

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

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _toggle_view_cam1(self) -> None:
        self._camera_mode = "Single Cam 1"
        self._toggle_view_mode()

    def _toggle_view_cam2(self) -> None:
        self._camera_mode = "Single Cam 2"
        self._toggle_view_mode()
    
    def _toggle_view_dual(self) -> None:
        self._camera_mode = "Dual"
        self._toggle_view_mode()

    def _toggle_view_mode(self) -> None:
        if self._camera_mode == "Single Cam 1":
            # Show only camera 1
            self._cam1.setVisible(True)
            self._cam1.setEnabled(True)
            self._cam2.setVisible(False)
            self._cam2.setEnabled(False)

            # Hide port 2 and dual buttons
            self._port2_edit.setVisible(False)
            self._connect_all_btn.setVisible(False)
            self._disconnect_all_btn.setVisible(False)
            
            # Show dual and cam2 toggle button to allow switching back to dual and cam2 view
            self._toggle_view_dual_btn.setVisible(True)  # show dual toggle when in cam1 view
            self._toggle_view_cam2_btn.setVisible(True)  # show cam2 toggle when in cam1 view
            self.resize(640, 600)
            self._status_bar.showMessage("Single View - Camera 1")


        elif self._camera_mode == "Single Cam 2":
            # Show only camera 2
            self._cam1.setVisible(False)
            self._cam1.setEnabled(False)
            self._cam2.setVisible(True)
            self._cam2.setEnabled(True)
        
            # Hide port 1 and dual buttons
            self._port1_edit.setVisible(False)
            self._connect_all_btn.setVisible(False)
            self._disconnect_all_btn.setVisible(False)   
            
            # Show dual and cam1 toggle button to allow switching back to dual and cam2 view
            self._toggle_view_dual_btn.setVisible(True)  # show dual toggle when in cam2 view
            self._toggle_view_cam1_btn.setVisible(True)  # show cam1 toggle when in cam2 view
            
            self.resize(640, 600)
            self._status_bar.showMessage("Single View - Camera 2")

        elif self._camera_mode == "Dual":
            # Show both cameras
            self._cam1.setVisible(True)
            self._cam1.setEnabled(True)
            self._cam2.setVisible(True)
            self._cam2.setEnabled(True)
            
            # Show port 2 and dual buttons
            self._port1_edit.setVisible(True)
            self._port2_edit.setVisible(True)
            self._connect_all_btn.setVisible(True)
            self._disconnect_all_btn.setVisible(True)
            
            # Show cam2 and cam1 toggle button to allow switching back to cam2 and cam1 view
            self._toggle_view_cam1_btn.setVisible(True)  # show cam1 toggle when in dual  view
            self._toggle_view_cam2_btn.setVisible(True)  # show cam2 toggle when in dual view

            self.resize(1280, 600)
            self._status_bar.showMessage("Dual View")


    def _on_connect_all(self) -> None:
        url1, url2 = self._build_urls()
        self._cam1.connect(url1)
        self._cam2.connect(url2)
        self._connect_all_btn.setEnabled(False)
        self._disconnect_all_btn.setEnabled(True)
        self._status_bar.showMessage(
            f"Connecting  |  Cam1 → {url1}  |  Cam2 → {url2}"
        )

    def _on_disconnect_all(self) -> None:
        self._cam1.disconnect()
        self._cam2.disconnect()
        self._connect_all_btn.setEnabled(True)
        self._disconnect_all_btn.setEnabled(False)
        self._status_bar.showMessage("Both cameras disconnected")

    # ------------------------------------------------------------------
    # Propagate close to panels so mpv shuts down cleanly
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._cam1.closeEvent(event)
        self._cam2.closeEvent(event)
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="ROV Dual Camera Viewer")
    parser.add_argument("--ip",    default=DEFAULT_IP,
                        help="ROV IP address (default: %(default)s)")
    parser.add_argument("--port1", type=int, default=DEFAULT_PORT1,
                        help="Camera 1 port (default: %(default)s)")
    parser.add_argument("--port2", type=int, default=DEFAULT_PORT2,
                        help="Camera 2 port (default: %(default)s)")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("ROV Dual Viewer")

    window = ROVDualViewer(ip=args.ip, port1=args.port1, port2=args.port2)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()