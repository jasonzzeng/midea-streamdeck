#!/usr/bin/env python3
"""Last-resort diagnostics for a V3 device whose getToken returns 9999.

Dumps the raw cloud record for the AC, then tries every plausible udpid
derivation against the token endpoint.

    ./diagnose.sh --account you@example.com
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from msmart.cloud import CloudError
from msmart.discover import Discover
from msmart.lan import Security

from setup import CLOUDS, read_password


def udpid_candidates(device) -> dict[str, bytes]:
    """Every id-encoding the cloud might have keyed the token under."""
    did, sn = device.id, (device.sn or "")
    cands = {
        "id 6-byte little": did.to_bytes(6, "little"),
        "id 6-byte big": did.to_bytes(6, "big"),
        "id 8-byte little": did.to_bytes(8, "little"),
        "id 8-byte big": did.to_bytes(8, "big"),
        "id as ascii": str(did).encode(),
    }
    if sn:
        cands["sn ascii"] = sn.encode()
        # The SN embeds the MAC; try the trailing MAC bytes both ways.
        tail = sn[-16:-4]
        try:
            mac = bytes.fromhex(tail)
            cands["mac from sn"] = mac
            cands["mac from sn reversed"] = mac[::-1]
        except ValueError:
            pass
    return cands


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("host", nargs="?", default="192.168.68.119")
    parser.add_argument("--account", required=True)
    parser.add_argument("--password")
    parser.add_argument("--password-file")
    parser.add_argument("--region", default="US")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    device = await Discover.discover_single(args.host, auto_connect=False)
    if not device:
        raise SystemExit(f"No device at {args.host}")
    print(f"Device: id={device.id} sn={device.sn} type={hex(device.type)} "
          f"version=V{device.version}\n")

    password = read_password(args)

    for name in ("mideaair", "nethome"):
        print(f"===== {name} =====")
        cloud = CLOUDS[name](args.region, account=args.account, password=password)
        try:
            await cloud.login()
        except CloudError as e:
            print(f"  login failed: {e}\n")
            continue

        # 1. Raw cloud record - shows every field, not just id/name.
        try:
            result = await cloud._api_request(
                "/v1/appliance/user/list/get", cloud._build_request_body({}))
            print("  Raw appliance record(s):")
            for a in (result or {}).get("list", []):
                print("   ", json.dumps(a, default=str))
        except Exception as e:  # noqa: BLE001
            print(f"  appliance list failed: {e}")

        # 2. Sweep udpid derivations.
        print("  Token attempts:")
        for label, raw in udpid_candidates(device).items():
            udpid = Security.udpid(raw).hex()
            try:
                token, key = await cloud.get_token(udpid)
            except CloudError as e:
                print(f"    {label:24s} -> {e}")
                continue
            print(f"    {label:24s} -> GOT TOKEN")
            try:
                await device.authenticate(token, key)
                print(f"\n*** SUCCESS: cloud={name} udpid={label} ***")
                print(json.dumps({"token": token, "key": key}, indent=2))
                return
            except Exception as e:  # noqa: BLE001
                print(f"      but the AC rejected it: {e}")
        print()

    print("No udpid derivation produced a working token.")


if __name__ == "__main__":
    asyncio.run(main())
