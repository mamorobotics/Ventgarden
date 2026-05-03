"""
Underwater Float — Simulated Depth vs Time (Fixed)
===================================================
Simulates a float diving and surfacing.
Shows both true depth and accelerometer-integrated estimated depth.

Setup:
  conda activate ventgarden
  pip install matplotlib numpy

Usage:
  python float_depth_sim.py
"""

import threading
import time
import math
import numpy as np
import matplotlib
matplotlib.use("TkAgg")          # explicit backend — avoids axis glitches on some systems
import matplotlib.pyplot as plt

# ─── DIVE PROFILE ─────────────────────────────────────────────────────────────
# List of (duration_seconds, target_depth_metres)
# The float smoothly moves between depths using a cosine curve

DIVE_PROFILE = [
    (3,  0.0),   # sit at surface
    (6,  2.0),   # dive to 2 m
    (4,  2.0),   # hover at 2 m
    (5,  5.0),   # dive to 5 m
    (5,  5.0),   # hover at 5 m
    (4,  3.0),   # rise to 3 m
    (3,  3.0),   # hover at 3 m
    (6,  0.0),   # surface
    (3,  0.0),   # sit at surface
]

NOISE_STD      = 0.02    # m/s² accelerometer noise (keep low to reduce drift)
SIM_HZ         = 50      # simulation sample rate
GRAVITY        = 9.81    # m/s²

# ─── TRUE DEPTH FUNCTION ──────────────────────────────────────────────────────

TOTAL_TIME = sum(d for d, _ in DIVE_PROFILE)

def true_depth(t):
    """Return the exact ground-truth depth (m) at time t (s)."""
    cursor   = 0.0
    prev_dep = DIVE_PROFILE[0][1]
    for duration, target in DIVE_PROFILE:
        if t <= cursor + duration:
            frac   = (t - cursor) / duration if duration > 0 else 1.0
            smooth = (1.0 - math.cos(math.pi * frac)) / 2.0
            return prev_dep + (target - prev_dep) * smooth
        cursor   += duration
        prev_dep  = target
    return prev_dep

# ─── SHARED DATA ──────────────────────────────────────────────────────────────

data_lock   = threading.Lock()
t_list      = []       # elapsed time (s)
true_list   = []       # ground-truth depth (m)
est_list    = []       # integrated estimated depth (m)
sim_done    = threading.Event()

# ─── SIMULATION THREAD ────────────────────────────────────────────────────────

def run_sim():
    dt       = 1.0 / SIM_HZ
    velocity = 0.0
    est_dep  = 0.0
    rng      = np.random.default_rng()

    steps = int(TOTAL_TIME * SIM_HZ) + 1

    for i in range(steps):
        t = i * dt

        # Ground truth at this step and neighbours (for numerical derivative)
        d0 = true_depth(t)
        d1 = true_depth(t + dt)
        d2 = true_depth(t + 2 * dt)

        # True vertical velocity and acceleration
        v0 = (d1 - d0) / dt
        v1 = (d2 - d1) / dt
        a0 = (v1 - v0) / dt        # m/s² downward positive

        # Simulated accelerometer reading:
        #   az = gravity + downward_accel + noise
        az_measured = GRAVITY + a0 + rng.normal(0, NOISE_STD)

        # Integrate: remove gravity to get net acceleration
        a_net     = az_measured - GRAVITY
        velocity += a_net * dt
        est_dep  += velocity * dt

        with data_lock:
            t_list.append(t)
            true_list.append(d0)
            est_list.append(est_dep)

        time.sleep(dt)

    sim_done.set()

# ─── PLOT ─────────────────────────────────────────────────────────────────────

fig, ax = plt.subplots(figsize=(11, 5))
fig.patch.set_facecolor("#f8f9fa")
ax.set_facecolor("#ffffff")
ax.grid(True, linestyle="--", alpha=0.4, zorder=0)

line_est,  = ax.plot([], [], color="#1a7abf", linewidth=2.0,
                     label="Estimated depth (integrated accel)", zorder=3)
line_true, = ax.plot([], [], color="#e05c2a", linewidth=1.5,
                     linestyle="--", label="True depth (ground truth)", zorder=4)

ax.set_xlabel("Time (s)", fontsize=12)
ax.set_ylabel("Depth (m)", fontsize=12)
ax.set_title("Underwater Float — Depth vs Time (Simulation)", fontsize=13, fontweight="bold")
ax.legend(loc="lower right", fontsize=10)

# Set axes once up front — never touch them again so invert stays intact
ax.set_xlim(0, TOTAL_TIME + 1)
ax.set_ylim(6.5, -0.5)           # inverted: 0 m at top, deeper values lower

info = ax.text(
    0.02, 0.06, "Starting…", transform=ax.transAxes,
    fontsize=10, color="#0c447c",
    bbox=dict(boxstyle="round,pad=0.3", facecolor="#eaf4fb", edgecolor="#aad4f0")
)


def update(frame):
    with data_lock:
        if not t_list:
            return
        t  = list(t_list)
        td = list(true_list)
        ed = list(est_list)

    line_true.set_data(t, td)
    line_est.set_data(t, ed)

    err = abs(ed[-1] - td[-1])
    info.set_text(
        f"t = {t[-1]:.1f} s  |  "
        f"Estimated: {ed[-1]:.3f} m  |  "
        f"True: {td[-1]:.3f} m  |  "
        f"Error: {err:.3f} m"
    )

    if sim_done.is_set():
        timer.stop()
        ax.set_title(
            "Underwater Float — Depth vs Time (Simulation complete)",
            fontsize=13, fontweight="bold"
        )
        save_plot()

    fig.canvas.draw_idle()


timer = fig.canvas.new_timer(interval=50)
timer.add_callback(update, None)

def save_plot():
    fname = f"float_depth_sim_{int(time.time())}.png"
    fig.savefig(fname, dpi=150, bbox_inches="tight")
    print(f"Plot saved to {fname}")

fig.canvas.mpl_connect("close_event", lambda e: sim_done.set())

# ─── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Simulating {TOTAL_TIME:.0f} s dive profile …")
    t = threading.Thread(target=run_sim, daemon=True)
    t.start()
    timer.start()
    plt.tight_layout()
    plt.show()
