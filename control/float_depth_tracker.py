"""
Underwater Float — Depth vs Time Tracker
=========================================
Receives accelerometer (ax, ay, az) data from the float over Bluetooth,
integrates to estimate depth, and plots depth vs time in real time.

Setup:
  conda activate ventgarden
  pip install bleak matplotlib numpy

Usage:
  1. Set DEVICE_ADDRESS and CHAR_UUID below.
  2. Run:  python float_depth_tracker.py
  3. Press Ctrl-C or close the plot window to stop and save the final graph.
"""

import asyncio
import threading
import time
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation
from bleak import BleakClient

# ─── CONFIGURATION ────────────────────────────────────────────────────────────

DEVICE_ADDRESS = "XX:XX:XX:XX:XX:XX"   # ← Replace with your float's BT MAC address
CHAR_UUID      = "0000ffe1-0000-1000-8000-00805f9b34fb"  # ← Replace with your characteristic UUID

GRAVITY        = 9.81        # m/s²  — standard gravity
SAMPLE_RATE_HZ = 50          # Hz    — expected samples per second (for display only)
MAX_POINTS     = 500         # number of data points to keep on the rolling plot
PLOT_INTERVAL  = 50          # ms    — how often to redraw the plot

# Calibration: how many seconds of still data to collect at startup to zero the bias
CALIBRATION_SECONDS = 2.0

# ─── SHARED STATE (written by BT thread, read by plot thread) ─────────────────

lock         = threading.Lock()
times        = []      # elapsed seconds
depths       = []      # metres below start (positive = deeper)
raw_accel    = []      # (t, ax, ay, az) tuples — kept for the saved CSV

_velocity    = 0.0     # running vertical velocity (m/s)
_depth       = 0.0     # running depth (m)
_last_time   = None    # time of previous sample
_az_bias     = 0.0     # vertical-axis bias removed after calibration
_calibrating = True
_cal_buffer  = []      # accumulates az samples during calibration
_start_time  = None

# ─── BLUETOOTH CALLBACK ───────────────────────────────────────────────────────

def handle_notification(sender, data: bytearray):
    """
    Called on every BLE notification from the float.
    Expects the float to send a UTF-8 string like:  "ax,ay,az\n"
    where ax/ay/az are floats in m/s².
    Adjust the parsing block below if your format differs.
    """
    global _velocity, _depth, _last_time, _az_bias, _calibrating, _start_time

    try:
        # ── Parse incoming bytes ──────────────────────────────────────────────
        text = data.decode("utf-8").strip()
        parts = text.split(",")
        if len(parts) < 3:
            return                          # malformed packet — skip
        ax, ay, az = float(parts[0]), float(parts[1]), float(parts[2])

    except (ValueError, UnicodeDecodeError):
        return                              # ignore bad packets

    now = time.monotonic()

    with lock:
        if _start_time is None:
            _start_time = now

        elapsed = now - _start_time

        # ── Calibration phase: collect az samples and compute bias ────────────
        if _calibrating:
            _cal_buffer.append(az)
            if elapsed >= CALIBRATION_SECONDS:
                _az_bias     = float(np.mean(_cal_buffer)) - GRAVITY
                _calibrating = False
                _last_time   = now
                print(f"[calibration done]  az_bias = {_az_bias:+.4f} m/s²")
            return                          # don't integrate during calibration

        # ── Integration ───────────────────────────────────────────────────────
        dt = now - _last_time
        _last_time = now

        if dt <= 0 or dt > 1.0:            # ignore stale / wildly delayed samples
            return

        # Net downward acceleration (positive = sinking)
        a_net = az - _az_bias - GRAVITY

        # Simple Euler integration  (swap sign if your Z-axis points up)
        _velocity += a_net * dt
        _depth    += _velocity * dt        # positive = below start point

        # ── Store ─────────────────────────────────────────────────────────────
        times.append(elapsed)
        depths.append(_depth)
        raw_accel.append((elapsed, ax, ay, az))

        # Rolling window — keep only the last MAX_POINTS
        if len(times) > MAX_POINTS:
            times.pop(0)
            depths.pop(0)


# ─── ASYNC BLE LISTENER ───────────────────────────────────────────────────────

async def ble_listen():
    print(f"Connecting to {DEVICE_ADDRESS} …")
    async with BleakClient(DEVICE_ADDRESS) as client:
        print("Connected.  Waiting for accelerometer data …")
        print(f"Calibrating for {CALIBRATION_SECONDS} s — keep the float still!\n")
        await client.start_notify(CHAR_UUID, handle_notification)
        # Run until the main thread signals shutdown
        while not _shutdown_event.is_set():
            await asyncio.sleep(0.1)
        await client.stop_notify(CHAR_UUID)

_shutdown_event = threading.Event()

def ble_thread_main():
    asyncio.run(ble_listen())


# ─── MATPLOTLIB ANIMATION ─────────────────────────────────────────────────────

fig, ax_plot = plt.subplots(figsize=(10, 5))
fig.patch.set_facecolor("#f8f9fa")
ax_plot.set_facecolor("#ffffff")

line, = ax_plot.plot([], [], color="#1a7abf", linewidth=1.8, label="Depth")
ax_plot.invert_yaxis()                     # deeper = lower on screen
ax_plot.set_xlabel("Time (s)", fontsize=12)
ax_plot.set_ylabel("Depth (m)", fontsize=12)
ax_plot.set_title("Underwater Float — Depth vs Time", fontsize=14, fontweight="bold")
ax_plot.grid(True, linestyle="--", alpha=0.5)
ax_plot.legend(loc="upper right")

depth_text = ax_plot.text(
    0.02, 0.05, "", transform=ax_plot.transAxes,
    fontsize=11, color="#333333",
    bbox=dict(boxstyle="round,pad=0.3", facecolor="#eaf4fb", edgecolor="#aad4f0")
)


def init_plot():
    line.set_data([], [])
    return (line,)


def update_plot(frame):
    with lock:
        if _calibrating:
            depth_text.set_text("Calibrating …")
            return (line, depth_text)

        if not times:
            return (line, depth_text)

        t_data = list(times)
        d_data = list(depths)

    line.set_data(t_data, d_data)

    # Auto-scale axes with a small margin
    t_min, t_max = t_data[0], t_data[-1]
    d_min, d_max = min(d_data), max(d_data)
    t_pad = max(1.0, (t_max - t_min) * 0.05)
    d_pad = max(0.1, abs(d_max - d_min) * 0.1)

    ax_plot.set_xlim(t_min - t_pad, t_max + t_pad)
    ax_plot.set_ylim(d_max + d_pad, d_min - d_pad)   # inverted axis

    current_depth = d_data[-1]
    depth_text.set_text(f"Current depth: {current_depth:+.3f} m")

    return (line, depth_text)


ani = animation.FuncAnimation(
    fig, update_plot, init_func=init_plot,
    interval=PLOT_INTERVAL, blit=True, cache_frame_data=False
)


def on_close(event):
    """Save data and signal BLE thread to stop when the window is closed."""
    _shutdown_event.set()
    save_data()


fig.canvas.mpl_connect("close_event", on_close)


# ─── DATA EXPORT ──────────────────────────────────────────────────────────────

def save_data():
    if not raw_accel:
        print("No data to save.")
        return

    filename = f"float_depth_{int(time.time())}.csv"
    with open(filename, "w") as f:
        f.write("time_s,ax_ms2,ay_ms2,az_ms2\n")
        for row in raw_accel:
            f.write(f"{row[0]:.4f},{row[1]:.4f},{row[2]:.4f},{row[3]:.4f}\n")

    fig_filename = filename.replace(".csv", ".png")
    fig.savefig(fig_filename, dpi=150, bbox_inches="tight")
    print(f"\nData saved to  : {filename}")
    print(f"Plot saved to  : {fig_filename}")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Start BLE listener in a background thread
    bt = threading.Thread(target=ble_thread_main, daemon=True)
    bt.start()

    # matplotlib must run on the main thread
    try:
        plt.tight_layout()
        plt.show()
    except KeyboardInterrupt:
        pass
    finally:
        _shutdown_event.set()
        save_data()
        print("Done.")
