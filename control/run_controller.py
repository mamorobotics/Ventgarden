"""
run_controller.py
Loads config.json and runs the controller -> serial bridge.

Usage:
    python3 run_controller.py
    python3 run_controller.py --config path/to/other_config.json
"""

import argparse
import json
import sys

from controller_serial import ControllerSerialBridge


def load_config(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Config file not found: {path}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in config: {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="ROV controller -> serial bridge")
    parser.add_argument(
        "--config", default="config.json",
        help="Path to config JSON (default: config.json)"
    )
    args = parser.parse_args()

    config = load_config(args.config)
    bridge = ControllerSerialBridge(config)
    bridge.start()


if __name__ == "__main__":
    main()
