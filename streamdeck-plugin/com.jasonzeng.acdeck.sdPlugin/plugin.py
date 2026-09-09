#!/usr/bin/env python3
"""Stream Deck plugin for a Midea Duo portable AC.

Keys show live room temperature and setpoint. One shared poller keeps every
visible key in sync, so N keys still cost one cloud request per interval.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))

import websockets

import midea_cloud
from ac import load_config, to_celsius, to_fahrenheit
from msmart.device import AirConditioner as AC

POLL_SECONDS = 20
# Stream Deck restarts us on crash; keep retry pressure off the cloud.
ERROR_BACKOFF = 30

POWER = "com.jasonzeng.acdeck.power"
TEMP_UP = "com.jasonzeng.acdeck.tempup"
TEMP_DOWN = "com.jasonzeng.acdeck.tempdown"
STATUS = "com.jasonzeng.acdeck.status"

logging.basicConfig(
    filename=PROJECT / "streamdeck-plugin" / "plugin.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
# httpx logs every cloud request at INFO; that would fill the log with one
# line per poll forever.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
_LOGGER = logging.getLogger("acdeck")


class ACDeckPlugin:
    def __init__(self, ws, uuid: str) -> None:
        self._ws = ws
        self._uuid = uuid
        self._cfg = load_config()
        self._contexts: dict[str, str] = {}   # context -> action UUID
        self._device: AC | None = None
        self._state: dict | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()

    # ---------------------------------------------------------------- device

    async def _connect(self) -> AC:
        account = self._cfg["account"]
        password = (midea_cloud.keychain_get(account) or "")
        if not password:
            raise RuntimeError(f"No Keychain password for {account}")
        session = await midea_cloud.connect_cloud(account, password)
        device = AC(ip=self._cfg.get("ip", "0.0.0.0"),
                    port=self._cfg.get("port", 6444),
                    device_id=int(self._cfg["device_id"]))
        device.beep = bool((self._cfg.get("defaults") or {}).get("beep", False))
        device._lan = midea_cloud.CloudLAN(session, device.id)
        return device

    async def _device_handle(self, fresh: bool = False) -> AC:
        if self._device is None or fresh:
            self._device = await self._connect()
        return self._device

    async def refresh(self) -> None:
        """Pull current state, retrying once with a fresh session."""
        async with self._lock:
            for attempt in (0, 1):
                try:
                    device = await self._device_handle(fresh=bool(attempt))
                    await device.refresh()
                    if device.online:
                        self._state = {
                            "power": device.power_state,
                            "target": to_fahrenheit(device.target_temperature),
                            "room": to_fahrenheit(device.indoor_temperature),
                            "mode": device.operational_mode.name,
                        }
                        return
                except Exception as e:  # noqa: BLE001
                    _LOGGER.warning("refresh failed (attempt %d): %s", attempt, e)
                    # A stale cached session looks exactly like an offline device.
                    midea_cloud.clear_session_cache()
            self._state = None

    async def apply(self, **changes) -> None:
        async with self._lock:
            try:
                device = await self._device_handle()
                if not device.online:
                    await device.refresh()
                if changes.get("power") is not None:
                    device.power_state = changes["power"]
                if changes.get("target_f") is not None:
                    device.target_temperature = to_celsius(changes["target_f"])
                    device.power_state = True
                await device.apply()
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("apply failed: %s", e)

    # ------------------------------------------------------------ stream deck

    async def _send(self, payload: dict) -> None:
        await self._ws.send(json.dumps(payload))

    async def set_title(self, context: str, title: str) -> None:
        await self._send({"event": "setTitle", "context": context,
                          "payload": {"title": title, "target": 0}})

    async def set_state(self, context: str, state: int) -> None:
        await self._send({"event": "setState", "context": context,
                          "payload": {"state": state}})

    def _title_for(self, action: str) -> str:
        s = self._state
        if s is None:
            return "—"
        if action == POWER:
            return f"{'ON' if s['power'] else 'OFF'}\n{s['room']}°F" if s["room"] \
                else ("ON" if s["power"] else "OFF")
        if action in (TEMP_UP, TEMP_DOWN):
            return f"{s['target']}°F" if s["target"] is not None else "—"
        if action == STATUS:
            room = f"{s['room']}°F" if s["room"] is not None else "—"
            if not s["power"]:
                return f"Room\n{room}\nOFF"
            return f"Room {room}\n→ {s['target']}°F\n{s['mode'].title()}"
        return ""

    async def render(self) -> None:
        for context, action in list(self._contexts.items()):
            try:
                await self.set_title(context, self._title_for(action))
                if action == POWER and self._state is not None:
                    await self.set_state(context, 1 if self._state["power"] else 0)
            except Exception as e:  # noqa: BLE001
                _LOGGER.warning("render failed: %s", e)

    # ----------------------------------------------------------------- loops

    async def poll_loop(self) -> None:
        while True:
            if self._contexts:
                await self.refresh()
                await self.render()
            delay = POLL_SECONDS if self._state is not None else ERROR_BACKOFF
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def on_key_up(self, action: str, context: str) -> None:
        if self._state is None:
            await self.refresh()

        if action == POWER:
            new = not (self._state or {}).get("power", False)
            await self.apply(power=new)
        elif action in (TEMP_UP, TEMP_DOWN):
            current = (self._state or {}).get("target")
            if current is None:
                await self.refresh()
                current = (self._state or {}).get("target") or 72
            step = 1 if action == TEMP_UP else -1
            await self.apply(target_f=max(60, min(86, current + step)))

        await self.refresh()
        await self.render()

    async def handle(self, message: dict) -> None:
        event = message.get("event")
        context = message.get("context")
        action = message.get("action")

        if event == "willAppear":
            self._contexts[context] = action
            if self._state is not None:
                await self.set_title(context, self._title_for(action))
            self._wake.set()
        elif event == "willDisappear":
            self._contexts.pop(context, None)
        elif event == "keyUp":
            await self.on_key_up(action, context)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-port", required=True)
    parser.add_argument("-pluginUUID", required=True)
    parser.add_argument("-registerEvent", required=True)
    parser.add_argument("-info", required=False)
    args, _ = parser.parse_known_args()

    uri = f"ws://127.0.0.1:{args.port}"
    _LOGGER.info("connecting to %s", uri)
    async with websockets.connect(uri, max_size=None) as ws:
        await ws.send(json.dumps({"event": args.registerEvent,
                                  "uuid": args.pluginUUID}))
        plugin = ACDeckPlugin(ws, args.pluginUUID)
        poller = asyncio.create_task(plugin.poll_loop())
        try:
            async for raw in ws:
                try:
                    await plugin.handle(json.loads(raw))
                except Exception as e:  # noqa: BLE001
                    _LOGGER.exception("handler error: %s", e)
        finally:
            poller.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        _LOGGER.exception("plugin exited")
        raise
