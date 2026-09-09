#!/usr/bin/env python3
"""Control a Midea air conditioner (MAP14AS1TWT-C) over the LAN.

Usage:
    ac.py on | off | toggle | status
    ac.py temp <degrees>
    ac.py mode <cool|heat|dry|fan_only|auto>
    ac.py fan  <auto|low|medium|high|max|silent>
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from msmart.device import AirConditioner as AC

import midea_cloud

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_CACHE = ROOT / ".last_state.json"


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(
            f"No config at {CONFIG_PATH}.\n"
            f"Copy config.example.json to config.json and fill it in, "
            f"or run ./setup.sh to discover the AC automatically."
        )
    return json.loads(CONFIG_PATH.read_text())


def build_device(cfg: dict) -> AC:
    return AC(ip=cfg.get("ip", "0.0.0.0"),
              port=cfg.get("port", 6444),
              device_id=int(cfg["device_id"]))


async def connect_via_cloud(cfg: dict, use_cache: bool = True) -> AC:
    """Control the AC through Midea's relay.

    Used when the unit has no LAN token -- see midea_cloud for why that
    happens on newer hardware.
    """
    account = cfg.get("account")
    if not account:
        sys.exit("config.json is set to cloud transport but has no 'account'.")

    password = midea_cloud.keychain_get(account) or os.environ.get("MIDEA_PASSWORD")
    if not password:
        sys.exit(
            f"No stored password for {account}.\n"
            f"Run ./setup.sh again to store it in the macOS Keychain, or set "
            f"MIDEA_PASSWORD."
        )

    session = await midea_cloud.connect_cloud(account, password, use_cache=use_cache)

    device = build_device(cfg)
    device._lan = midea_cloud.CloudLAN(session, device.id)
    await device.refresh()
    return device


async def connect(cfg: dict) -> AC:
    if cfg.get("transport") == "cloud":
        device = await connect_via_cloud(cfg)
        if not device.online:
            # A stale cached session looks exactly like an offline device.
            midea_cloud.clear_session_cache()
            device = await connect_via_cloud(cfg, use_cache=False)
        if not device.online:
            sys.exit("The AC did not respond via the Midea cloud.")
        return device

    device = build_device(cfg)
    if cfg.get("token") and cfg.get("key"):
        await device.authenticate(cfg["token"], cfg["key"])
    await device.refresh()
    if not device.online:
        sys.exit(f"AC at {cfg['ip']} is not responding.")
    return device


def apply_defaults(device: AC, cfg: dict) -> None:
    """Set a sane starting point when powering on from cold."""
    d = cfg.get("defaults") or {}
    if mode := d.get("mode"):
        device.operational_mode = AC.OperationalMode[mode.upper()]
    if (temp := d.get("target_temperature")) is not None:
        device.target_temperature = to_celsius(float(temp))
    if fan := d.get("fan_speed"):
        device.fan_speed = AC.FanSpeed[fan.upper()]


def to_celsius(value: float) -> float:
    """Accept Fahrenheit-looking numbers and convert; pass Celsius through."""
    return round((value - 32) * 5 / 9, 1) if value > 45 else value


def to_fahrenheit(celsius: float | None) -> float | None:
    return None if celsius is None else round(celsius * 9 / 5 + 32)


def describe(device: AC) -> str:
    power = "ON" if device.power_state else "OFF"
    target = to_fahrenheit(device.target_temperature)
    indoor = to_fahrenheit(device.indoor_temperature)
    parts = [f"power={power}"]
    if device.power_state:
        parts.append(f"mode={device.operational_mode.name}")
        parts.append(f"target={target}F")
        parts.append(f"fan={getattr(device.fan_speed, 'name', device.fan_speed)}")
    if indoor is not None:
        parts.append(f"room={indoor}F")
    return "  ".join(parts)


async def main(argv: list[str]) -> None:
    if not argv:
        sys.exit(__doc__)

    command, args = argv[0].lower(), argv[1:]
    cfg = load_config()
    device = await connect(cfg)

    if command == "status":
        print(describe(device))
        return

    if command == "on":
        if not device.power_state:
            apply_defaults(device, cfg)
        device.power_state = True
    elif command == "off":
        device.power_state = False
    elif command == "toggle":
        if not device.power_state:
            apply_defaults(device, cfg)
        device.power_state = not device.power_state
    elif command == "temp":
        device.target_temperature = to_celsius(float(args[0]))
        device.power_state = True
    elif command == "mode":
        device.operational_mode = AC.OperationalMode[args[0].upper()]
        device.power_state = True
    elif command == "fan":
        device.fan_speed = AC.FanSpeed[args[0].upper()]
    else:
        sys.exit(f"Unknown command: {command}\n{__doc__}")

    await device.apply()
    print(describe(device))
    STATE_CACHE.write_text(json.dumps({"power": device.power_state}))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
