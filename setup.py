#!/usr/bin/env python3
"""Discover the Midea AC on the LAN and write config.json.

    ./setup.sh                                   # broadcast discovery, no cloud
    ./setup.sh 192.168.68.119                    # probe one IP directly
    ./setup.sh 192.168.68.119 --account you@example.com

V3 devices need a token+key issued by the cloud. Midea runs several apps
against the same servers ("Midea Air", "NetHome Plus", "Midea SmartHome") and
the token store is keyed per app, so the token has to be requested using the
identity of the app the AC was actually set up in. By default this tries each
one until a token comes back.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from msmart.cloud import CloudError
from msmart.device import AirConditioner as AC
from msmart.discover import Discover
from msmart.lan import Security

import midea_cloud
from midea_cloud import CLOUDS

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

DEFAULTS = {"mode": "COOL", "target_temperature": 70, "fan_speed": "AUTO"}


APPLIANCE_LIST_ENDPOINTS = {
    "mideaair": "/v1/appliance/user/list/get",
    "nethome": "/v1/appliance/user/list/get",
    "smarthome": "/v1/appliance/home/list/get",
}


def read_password(args) -> str:
    """Resolve the password without requiring a TTY.

    Order: --password, --password-file, $MIDEA_PASSWORD, the macOS Keychain,
    then an interactive prompt. The prompt needs a real terminal; Claude Code's `!` prefix and most
    CI shells do not provide one, hence the first three options.
    """
    if args.password:
        return args.password

    if args.password_file:
        path = Path(args.password_file).expanduser()
        if not path.exists():
            raise SystemExit(f"Password file not found: {path}")
        return path.read_text().splitlines()[0].strip()

    if env := os.environ.get("MIDEA_PASSWORD"):
        return env

    if args.account and (stored := midea_cloud.keychain_get(args.account)):
        return stored

    if not sys.stdin.isatty():
        raise SystemExit(
            "\nNo terminal available to prompt for the password.\n"
            "Pick one of these instead:\n\n"
            "  1. Open Terminal.app and run this command there (it will prompt).\n\n"
            "  2. Put the password in a file, readable only by you:\n"
            "       touch ~/.midea_password && chmod 600 ~/.midea_password\n"
            "       ./setup.sh <ip> --account <email> --password-file ~/.midea_password\n\n"
            "  3. Export it first:  export MIDEA_PASSWORD='...'\n\n"
            "The password is only used to fetch the token+key, and is never stored."
        )

    return getpass.getpass("Midea app password: ")


async def try_cloud(cloud_name, device, region, account, password):
    """Try to get a working token+key from one cloud. Returns None on failure."""
    cloud = CLOUDS[cloud_name](region, account=account, password=password)

    try:
        await cloud.login()
    except CloudError as e:
        print(f"  [{cloud_name}] login failed: {e}")
        return None

    print(f"  [{cloud_name}] login OK")

    # The token store is keyed per app, and msmart-ng sends applianceCodes
    # because the SmartHome cloud requires it. Vary both that and the byte
    # order of the udpid.
    for send_id in (True, False):
        for endian in ("little", "big"):
            udpid = Security.udpid(device.id.to_bytes(6, endian)).hex()
            try:
                token, key = await cloud.get_token(
                    udpid, device.id if send_id else None)
            except CloudError:
                continue
            try:
                await device.authenticate(token, key)
                print(f"  [{cloud_name}] token OK ({endian}-endian, "
                      f"applianceCodes={'yes' if send_id else 'no'}) -- authenticated")
                return token, key
            except Exception as e:  # noqa: BLE001
                print(f"  [{cloud_name}] got a token but the AC rejected it: {e}")

    print(f"  [{cloud_name}] no usable token (this is not the app the AC was set up in)")
    await show_devices(cloud, cloud_name, device.id)
    return None


async def show_devices(cloud, cloud_name, device_id) -> None:
    """Print the appliances this account owns, to confirm we are in the right place."""
    try:
        result = await cloud._api_request(
            APPLIANCE_LIST_ENDPOINTS[cloud_name], cloud._build_request_body({}))
    except Exception:  # noqa: BLE001
        return

    appliances = (result or {}).get("list", [])
    if not appliances:
        print(f"      (this account owns no devices on {cloud_name})")
        return
    for a in appliances:
        mark = "  <-- your AC" if str(a.get("id")) == str(device_id) else ""
        print(f"      owns: id={a.get('id')} name={a.get('name')}{mark}")


async def verify_cloud_control(device, account, password) -> bool:
    """Confirm the cloud relay can actually drive this AC before committing to it."""
    try:
        session = await midea_cloud.connect_cloud(account, password, use_cache=False)
    except Exception as e:  # noqa: BLE001
        print(f"  login failed: {e}")
        return False

    probe = AC(ip=device.ip, port=device.port, device_id=device.id)
    probe._lan = midea_cloud.CloudLAN(session, device.id)
    try:
        await probe.refresh()
    except Exception as e:  # noqa: BLE001
        print(f"  cloud relay failed: {e}")
        return False

    if not probe.online:
        print("  cloud relay returned no usable reply")
        return False

    temp = probe.indoor_temperature
    room = f"{round(temp * 9 / 5 + 32)}F" if temp is not None else "unknown"
    print(f"  cloud relay WORKS -- power="
          f"{'on' if probe.power_state else 'off'}, room={room}")
    return True


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?", help="IP of the AC (optional)")
    parser.add_argument("--account", help="Midea app account (email)")
    parser.add_argument("--password", help="Midea app password (prompted if omitted)")
    parser.add_argument("--password-file", help="File whose first line is the password")
    parser.add_argument("--cloud", choices=[*CLOUDS, "auto"], default="auto",
                        help="Which Midea app to authenticate as (default: try each)")
    parser.add_argument("--region", default="US")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING,
                        format="%(levelname)s: %(message)s")

    # Step 1: find the device. auto_connect=False keeps this entirely local.
    kwargs = {"auto_connect": False}
    if args.host:
        devices = [d for d in [await Discover.discover_single(args.host, **kwargs)] if d]
    else:
        devices = await Discover.discover(**kwargs)

    if not devices:
        raise SystemExit(
            "No Midea devices found.\n"
            "  - Confirm the AC is powered and on this same Wi-Fi network\n"
            "  - Try passing the IP directly: ./setup.sh 192.168.68.119"
        )

    device = devices[0]
    if len(devices) > 1:
        print(f"Found {len(devices)} devices; using {device.ip}.")

    print(f"Found: {device.name}  ip={device.ip}  id={device.id}  "
          f"sn={device.sn}  protocol=V{device.version}")

    # Step 2: V3 devices need a token/key from the cloud.
    token = key = None
    if device.version == 3:
        if not args.account:
            raise SystemExit(
                "\nThis is a V3 device, so it needs a token+key from the Midea cloud.\n"
                "Re-run with the account you use in the Midea app:\n"
                f"  ./setup.sh {device.ip} --account you@example.com"
            )
        password = read_password(args)
        candidates = list(CLOUDS) if args.cloud == "auto" else [args.cloud]

        print(f"\nTrying {len(candidates)} Midea app identity(s):")
        result = None
        for name in candidates:
            result = await try_cloud(name, device, args.region, args.account, password)
            if result:
                break

        if result:
            token, key = result
        else:
            # No LAN token exists for this unit. Fall back to the cloud relay,
            # which drives the same command frames over Midea's servers.
            print("\nNo LAN token available for this unit.")
            print("This is expected on newer hardware -- the Midea app's own")
            print("getToken call fails the same way. Falling back to the cloud relay:")

            if not await verify_cloud_control(device, args.account, password):
                raise SystemExit(
                    "\nNeither LAN nor cloud control worked. The account and AC "
                    "are correct,\nso this may be a temporary cloud problem -- "
                    "try again shortly."
                )

            try:
                midea_cloud.keychain_set(args.account, password)
                print(f"\nStored the password in the macOS Keychain "
                      f"(service '{midea_cloud.KEYCHAIN_SERVICE}').")
            except Exception as e:  # noqa: BLE001
                print(f"\nCould not write to the Keychain ({e}); "
                      f"set MIDEA_PASSWORD instead.")

            existing = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
            CONFIG_PATH.write_text(json.dumps({
                "transport": "cloud",
                "account": args.account,
                "ip": device.ip,
                "port": device.port,
                "device_id": device.id,
                "defaults": existing.get("defaults", DEFAULTS),
            }, indent=2) + "\n")
            CONFIG_PATH.chmod(0o600)
            print(f"Wrote {CONFIG_PATH} (transport=cloud)")
            print("Now try:  ./bin/ac-status.sh")
            return
    else:
        await device.refresh()

    # Step 3: persist.
    existing = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    config = {
        "ip": device.ip,
        "port": device.port,
        "device_id": device.id,
        "token": token,
        "key": key,
        "defaults": existing.get("defaults", DEFAULTS),
    }
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")
    print(f"\nWrote {CONFIG_PATH}")
    print("Now try:  ./bin/ac-status.sh")


if __name__ == "__main__":
    asyncio.run(main())
