"""
controller_serial.py
Reads gamepad input via pygame and sends it over serial to an Arduino,
mirroring the behaviour of the old Controller.cpp / Serial.cpp from Leviathan.

Wire format (matches old C++ toStringPartial / SendControllerAndGetFloatData):
  Send:    <ljoyx!ljoyy!rjoyx!rjoyy!ltrig!rtrig[!a][!b][!x][!y][!j][!k][!u][!d][!l][!r]>
  Reset:   |
  Receive: lines until '~' terminator; lines starting with 'RN' are float/sensor data
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional

import pygame
import serial


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ControllerValues:
    ljoyx: float = 0.0
    ljoyy: float = 0.0
    rjoyx: float = 0.0
    rjoyy: float = 0.0
    ltrigger: float = 0.0
    rtrigger: float = 0.0
    a: int = 0
    b: int = 0
    x: int = 0
    y: int = 0
    lbumper: int = 0
    rbumper: int = 0
    up: int = 0
    down: int = 0
    left: int = 0
    right: int = 0

    def to_string_partial(self) -> str:
        """
        Compact serialization matching the old C++ ControllerValues::toStringPartial().

        Axes     -> int in [-999, 999]  (original float * 1000, clamped)
        Triggers -> int in [0, 99]      (original float * 100, rounded, clamped)
        Buttons  -> single-letter codes appended only when pressed
                    a b x y  =  face buttons
                    j k      =  left / right bumper
                    u d l r  =  D-pad up / down / left / right
        """
        lx = max(-999, min(999, int(self.ljoyx * 1000)))
        ly = max(-999, min(999, int(self.ljoyy * 1000))) # I did CHANGE THIS #####LATER PLEASE - bypassed this and lx bc my controller has joystick drift
        rx = max(-999, min(999, int(self.rjoyx * 1000)))
        ry = max(-999, min(999, int(self.rjoyy * 1000)))
        lt = max(0, min(99, round(self.ltrigger * 100)))
        rt = max(0, min(99, round(self.rtrigger * 100)))
        


        s = f"{lx}!{ly}!{rx}!{ry}!{lt}!{rt}"

        if self.a:       s += "!a"
        if self.b:       s += "!b"
        if self.x:       s += "!x"
        if self.y:       s += "!y"
        if self.lbumper: s += "!j"
        if self.rbumper: s += "!k"
        if self.up:      s += "!u"
        if self.down:    s += "!d"
        if self.left:    s += "!l"
        if self.right:   s += "!r"

        return s


# ---------------------------------------------------------------------------
# Controller reader
# ---------------------------------------------------------------------------

class GameController:
    """
    Wraps pygame.joystick to read a gamepad and return ControllerValues.
    All axis/button indices are driven by the 'controller' section of config.json
    so the code works with any controller layout without code changes.
    """

    def __init__(self, config: dict) -> None:
        self.deadzone: float = config.get("deadzone", 0.05)
        self.axis_map: dict = config.get("axis_map", {})
        self.button_map: dict = config.get("button_map", {})
        self.invert_axes: dict = config.get("invert_axes", {})
        self.controller_id: int = config.get("id", 0)
        self.joystick: Optional[pygame.joystick.JoystickType] = None

    def connect(self) -> bool:
        pygame.init()
        pygame.joystick.init()
        count = pygame.joystick.get_count()
        if count == 0:
            print("[controller] No controllers detected.")
            return False
        if self.controller_id >= count:
            print(
                f"[controller] Controller {self.controller_id} not found "
                f"({count} available). Falling back to 0."
            )
            self.controller_id = 0
        self.joystick = pygame.joystick.Joystick(self.controller_id)
        self.joystick.init()
        print(f"[controller] Connected: {self.joystick.get_name()}")
        return True

    # -- helpers -------------------------------------------------------------

    def _deadzone(self, val: float) -> float:
        return 0.0 if abs(val) < self.deadzone else val

    def _axis(self, name: str) -> float:
        idx = self.axis_map.get(name)
        if idx is None or self.joystick is None:
            return 0.0
        try:
            val: float = self.joystick.get_axis(idx)
        except pygame.error:
            return 0.0
        if self.invert_axes.get(name, False):
            val = -val
        return self._deadzone(val)

    def _button(self, name: str) -> int:
        idx = self.button_map.get(name)
        if idx is None or self.joystick is None:
            return 0
        try:
            return int(self.joystick.get_button(idx))
        except pygame.error:
            return 0

    ## Misha edit --> reads D-pad from hat input instead of buttons, since my controller (PS4) reports D-pad as hat, not buttons
    def _get_dpad_from_hat(self) -> tuple[int, int, int, int]:
        """
        Read D-pad from hat input (POV).
        Returns (up, down, left, right) as 0 or 1.
        Hat values: (x, y) where x=[-1,0,1], y=[-1,0,1]
        """
        if self.joystick is None or self.joystick.get_numhats() == 0:
            return (0, 0, 0, 0)
        try:
            x, y = self.joystick.get_hat(0)
            up = 1 if y > 0 else 0
            down = 1 if y < 0 else 0
            left = 1 if x < 0 else 0
            right = 1 if x > 0 else 0
            return (up, down, left, right)
        except pygame.error:
            return (0, 0, 0, 0)

    @staticmethod
    def _normalize_trigger(val: float) -> float:
        """
        pygame reports Xbox triggers as [-1, 1] (rest = -1, fully pressed = 1).
        The old GLFW code read them as digital buttons (0 or 1).
        This normalizes to [0.0, 1.0] so triggers are analogue, which is an
        improvement — the Arduino firmware can use the full range if desired.
        """
        if val < 0.0:
            return (val + 1.0) / 2.0
        return float(val)

    # -- public --------------------------------------------------------------

    def get_values(self) -> ControllerValues:
        """Poll the joystick and return a populated ControllerValues."""
        pygame.event.pump()  # keep the pygame event queue healthy
        cv = ControllerValues()
        if self.joystick is None:
            return cv

        cv.ljoyx    = self._axis("ljoyx")
        cv.ljoyy    = self._axis("ljoyy")
        cv.rjoyx    = self._axis("rjoyx")
        cv.rjoyy    = self._axis("rjoyy")
        cv.ltrigger = self._normalize_trigger(self._axis("ltrigger"))
        cv.rtrigger = self._normalize_trigger(self._axis("rtrigger"))

        cv.a       = self._button("a")
        cv.b       = self._button("b")
        cv.x       = self._button("x")
        cv.y       = self._button("y")
        cv.lbumper = self._button("lbumper")
        cv.rbumper = self._button("rbumper")
        
        # Read D-pad from hat input
        cv.up, cv.down, cv.left, cv.right = self._get_dpad_from_hat()

        return cv


# ---------------------------------------------------------------------------
# Serial link
# ---------------------------------------------------------------------------

class SerialLink:
    """
    Manages the USB-serial connection to the Arduino.
    Mirrors Serial.cpp: send controller packet, wait for '~'-terminated response.
    """

    def __init__(self, config: dict) -> None:
        self.port: str       = config.get("port", "/dev/ttyUSB0")
        self.baud_rate: int  = config.get("baud_rate", 9600)
        self.timeout: float  = config.get("timeout", 1.0)
        self.ser: Optional[serial.Serial] = None
        self._float_outputs: List[str] = []
        self._lock = threading.Lock()

    def connect(self) -> bool:
        try:
            self.ser = serial.Serial(self.port, self.baud_rate, timeout=self.timeout)
            # Connecting via pyserial resets Arduino (DTR pulse). Wait for it to boot.
            print(f"[serial] Opened {self.port} @ {self.baud_rate} baud. Waiting for Arduino...")
            time.sleep(2.0)
            print("[serial] Ready.")
            return True
        except serial.SerialException as e:
            print(f"[serial] Could not open port: {e}")
            return False

    def send_reset(self) -> None:
        """Send the reset sentinel '|' — matches old code's one-time reset call."""
        if self.ser and self.ser.is_open:
            self.ser.write(b"|")

    def send_controller(self, values: ControllerValues) -> None:
        if self.ser and self.ser.is_open:
            print(values.to_string_partial())
            msg = f"<{values.to_string_partial()}>".encode()
            self.ser.write(msg)

    def read_responses(self) -> List[str]:
        """
        Read lines from the Arduino until a '~' terminator is received.
        Lines starting with 'RN' are float/sensor data and are stored.
        Returns all lines read in this cycle.
        """
        results: List[str] = []
        if not self.ser or not self.ser.is_open:
            return results

        while True:
            if not self.ser.in_waiting > 0:
                break
            raw = self.ser.readline()
            if not raw:
                # readline() timed out — Arduino may not have responded
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            if line.startswith("~"):
                break
            results.append(line)
            if line.startswith("RN"):
                with self._lock:
                    self._float_outputs.append(line)

        return results

    def get_float_outputs(self) -> List[str]:
        """Return a copy of all 'RN' lines received so far."""
        with self._lock:
            return list(self._float_outputs)

    def disconnect(self) -> None:
        if self.ser and self.ser.is_open:
            self.ser.close()
            print("[serial] Disconnected.")


# ---------------------------------------------------------------------------
# Bridge — ties controller and serial together
# ---------------------------------------------------------------------------

class ControllerSerialBridge:
    """
    Reads controller input and sends it over serial in a continuous loop,
    then reads and stores any response from the Arduino.
    """

    def __init__(self, config: dict) -> None:
        self.controller  = GameController(config["controller"])
        self.serial_link = SerialLink(config["serial"])
        self.send_interval: float = config["controller"].get("send_interval_ms", 50) / 1000.0
        self._running = False

    def start(self) -> None:
        if not self.controller.connect():
            raise RuntimeError("Failed to connect to controller.")
        if not self.serial_link.connect():
            raise RuntimeError("Failed to open serial port.")

        self.serial_link.send_reset()
        self._running = True
        self._loop()

    def stop(self) -> None:
        self._running = False
        self.serial_link.disconnect()

    def get_float_outputs(self) -> List[str]:
        return self.serial_link.get_float_outputs()

    def _loop(self) -> None:
        print("[bridge] Running. Press Ctrl+C to stop.")
        try:
            while self._running:
                values = self.controller.get_values()
                self.serial_link.send_controller(values)
                time.sleep(self.send_interval)
                responses = self.serial_link.read_responses()
                if responses:
                    for line in responses:
                        print(f"[arduino] {line}")
        except KeyboardInterrupt:
            print("\n[bridge] Interrupted.")
        finally:
            self.stop()
