#!/usr/bin/env python3
"""Cloud transport for Midea devices that have no LAN token.

Newer Midea units (the Matter-era MAP14 Duo among them) are never provisioned
with a LAN token, so msmart-ng's local protocol cannot authenticate: the cloud
answers getToken with a bare 9999. Those units are still fully controllable
through the same cloud endpoint the phone app uses --
/v1/appliance/transparent/send -- which relays the *identical* command frames
msmart-ng already knows how to build and parse.

This module supplies:
  * MideaAirCloud / NetHomePlusCloud -- app identities msmart-ng lacks
  * transparent_send()               -- the cloud relay itself
  * CloudLAN                         -- drop-in replacement for Device._lan
  * Keychain + session caching       -- so a Stream Deck press is one request
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from hashlib import md5, sha256
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import unquote_plus, urlencode

import httpx
from Crypto.Cipher import AES
from Crypto.Util import Padding
from msmart.cloud import ApiError, CloudError, NetHomePlusCloud, SmartHomeCloud
from msmart.lan import ProtocolError, Security, _Packet

ROOT = Path(__file__).resolve().parent
SESSION_CACHE = ROOT / ".session.json"
SESSION_MAX_AGE = 60 * 60 * 6  # re-login every 6h
KEYCHAIN_SERVICE = "ac-deck-midea"


class MideaAirCloud(NetHomePlusCloud):
    """The "Midea Air" app identity.

    Same host and request shape as NetHome Plus, different app id and key.
    msmart-ng does not ship this variant.
    """

    APP_ID = "1117"

    class _Security(NetHomePlusCloud._Security):
        APP_KEY = "ff0cf6f5f0c3471de36341cab3f7a9af"

    def __init__(self, region: str = "US", **kwargs) -> None:
        super().__init__(region, **kwargs)
        self._security = MideaAirCloud._Security()


CLOUDS = {
    "mideaair": MideaAirCloud,
    "nethome": NetHomePlusCloud,
    "smarthome": SmartHomeCloud,
}


# ---------------------------------------------------------------- encoding --

def encode_csv(data: bytes) -> str:
    """Midea sends command bytes as a CSV of *signed* integers."""
    return ",".join(str(b - 256 if b > 127 else b) for b in data)


def decode_csv(text: str) -> bytes:
    return bytes((int(v) + 256) % 256 for v in text.split(",") if v.strip())


def md5_app_key(app_key: str) -> str:
    """The key that unwraps the session's accessToken."""
    return md5(app_key.encode("utf-8")).hexdigest()[:16]


def aes_encrypt_string(key: str, plaintext: str) -> str:
    cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
    return cipher.encrypt(Padding.pad(plaintext.encode("utf-8"), 16)).hex()


def aes_decrypt_string(key: str, ciphertext_hex: str) -> str:
    cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
    raw = cipher.decrypt(bytes.fromhex(ciphertext_hex))
    return Padding.unpad(raw, 16).decode("utf-8")


def data_key_for(cloud) -> str:
    """Derive the per-session key used to encrypt relayed commands.

    Not md5(appkey) directly: that only unwraps the accessToken returned at
    login, and the *decrypted* token is the actual data key. Encrypting with
    md5(appkey) instead makes the cloud reject the command with 1011.
    """
    access_token = (cloud._session or {}).get("accessToken")
    if not access_token:
        raise CloudError("No accessToken in the session; log in first.")
    return aes_decrypt_string(md5_app_key(cloud._security.APP_KEY), access_token)


# The cloud relay carries a 0x5A5A packet whose inner command frame is
# PLAINTEXT, flagged by message type 0x0110 (the LAN protocol uses 0x0111 with
# an AES-encrypted payload). The 6-byte marker in the reserved field is copied
# from the app; the server rejects the packet without it.
CLOUD_MSG_TYPE = b"\x01\x10"
CLOUD_MESSAGE_ID = b"\x50\x00\x00\x00"
CLOUD_RESERVED = bytes.fromhex("b60502040504") + bytes(6)


def build_cloud_packet(device_id: int, frame: bytes) -> bytes:
    """Wrap a command frame the way the Midea Air app does for the relay."""
    length = 40 + len(frame) + 16
    header = b"\x5a\x5a" + CLOUD_MSG_TYPE + length.to_bytes(2, "little")
    header += b"\x20\x00"
    header += CLOUD_MESSAGE_ID
    header += _Packet._timestamp()
    header += device_id.to_bytes(8, "little")
    header += CLOUD_RESERVED
    packet = header + frame
    return packet + Security.sign(packet)


def parse_cloud_packet(packet: bytes) -> bytes:
    """Pull the command frame out of a relay reply (plaintext or encrypted)."""
    if packet[:2] != b"\x5a\x5a":
        return packet
    length = int.from_bytes(packet[4:6], "little")
    packet = packet[:length] if 0 < length <= len(packet) else packet
    payload = packet[40:-16]
    # Requests go out as 0x0110 and replies come back as 0x0100, both with the
    # frame in the clear; anything else is an encrypted LAN-style payload.
    if payload[:1] == b"\xaa":
        return payload
    return Security.decrypt_aes(payload)


def normalize_reply(reply: bytes) -> bytes:
    """Reduce a cloud reply to the bare 0xAA command frame msmart expects.

    The relay has been observed returning either a full 0x5A5A LAN packet or
    the frame already unwrapped, so handle both rather than guessing.
    """
    if reply[:2] == b"\x5a\x5a":
        try:
            return _Packet.decode(reply)
        except (ProtocolError, ValueError):
            return reply[40:]
    if reply[:1] == b"\xaa":
        return reply
    return reply[40:] if len(reply) > 40 else reply


# ------------------------------------------------------------------ relay --

async def transparent_send(cloud, appliance_id: int, data: bytes) -> bytes:
    """Relay one command frame to the device via the Midea cloud."""
    key = data_key_for(cloud)
    order = aes_encrypt_string(key, encode_csv(data))

    result = await cloud._api_request(
        "/v1/appliance/transparent/send",
        cloud._build_request_body({
            "order": order,
            "funId": "0000",
            "applianceId": str(appliance_id),
        }),
    )

    reply_hex = (result or {}).get("reply")
    if not reply_hex:
        raise CloudError("Cloud accepted the command but returned no reply.")

    return normalize_reply(decode_csv(aes_decrypt_string(key, reply_hex)))


# The endpoint, funId and field set below were taken from a capture of the
# Midea Air iOS app (5.20.0) controlling this exact AC. The legacy
# /v1/appliance/transparent/send that every public library uses answers 3176
# ("the asyn reply does not exist") for this hardware and never reaches the
# unit; the /new variant with funId FC02 is what actually works.
SEND_PATH = "/v1/appliance/transparent/send/new"
SEND_FUN_ID = "FC02"
SEND_SRC = "17"
SEND_FORMAT = "2"
SEND_LANGUAGE = "en-US"


def sign_body(app_key: str, path: str, params: dict) -> str:
    """sha256(path + sorted urlencoded params + app key), matching the app."""
    query = unquote_plus(urlencode(sorted(params.items())))
    return sha256((path + query + app_key).encode("ascii")).hexdigest()


def _base_params() -> dict:
    """The field set every /new endpoint expects (from the app's requests)."""
    return {
        "format": SEND_FORMAT,
        "language": SEND_LANGUAGE,
        "src": SEND_SRC,
        "stamp": datetime.now().strftime("%Y%m%d%H%M%S"),
    }


async def _post_form(path: str, params: dict, app_key: str) -> dict:
    params = dict(params)
    params["sign"] = sign_body(app_key, path, params)
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(
            f"https://mapp.appsmb.com{path}",
            data=params,
            headers={"User-Agent": "Midea OBM/5.20.0 (iPhone; iOS 18.7.8; Scale/3.00)",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
    r.raise_for_status()
    body = r.json()
    if str(body.get("errorCode", "0")) != "0":
        raise CloudError(f"Code: {body.get('errorCode')}, Message: {body.get('msg')}")
    return body.get("result") or {}


def app_key_and_iv(app_key: str) -> tuple[str, str]:
    """sha256(appkey) split into the key and IV that unwrap the session secrets."""
    digest = sha256(app_key.encode("ascii")).hexdigest()
    return digest[:16], digest[16:32]


def aes_cbc_decrypt(key: str, iv: str, hex_data: str) -> str:
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv=iv.encode("utf-8"))
    return Padding.unpad(cipher.decrypt(bytes.fromhex(hex_data)), 16).decode("utf-8")


def aes_cbc_encrypt(key: str, iv: str, text: str) -> str:
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv=iv.encode("utf-8"))
    return cipher.encrypt(Padding.pad(text.encode("utf-8"), 16)).hex()


class NewApiSession:
    """A session established through the app's /v1/user/login/new flow.

    The legacy /v1/user/login session is accepted by the legacy send endpoint,
    but its ECB data key is rejected (1011) by transparent/send/new. The newer
    login returns an extra randomData field, and the pair (accessToken,
    randomData) unwraps to an AES-CBC key and IV, which is what /new expects.
    """

    APP_KEY = MideaAirCloud._Security.APP_KEY
    APP_ID = "1117"

    def __init__(self, account: str, password: str) -> None:
        self._account = account
        self._password = password
        self.session_id: Optional[str] = None
        self.data_key: Optional[str] = None
        self.data_iv: Optional[str] = None

    def _terminal_id(self) -> str:
        # Stable per account, so we don't look like a new device every login.
        return md5(f"ac-deck:{self._account}".encode("utf-8")).hexdigest()

    def _password_hash(self, login_id: str) -> str:
        inner = sha256(self._password.encode("ascii")).hexdigest()
        return sha256((login_id + inner + self.APP_KEY).encode("ascii")).hexdigest()

    def _iam_password_hash(self, login_id: str) -> str:
        inner = md5(md5(self._password.encode("ascii")).hexdigest().encode("ascii"))
        return sha256(
            (login_id + inner.hexdigest() + self.APP_KEY).encode("ascii")).hexdigest()

    def _login_params(self) -> dict:
        return _base_params() | {
            "appId": self.APP_ID,
            "clientType": "2",
            "encryptVersion": "1",
            "phoneModel": "iOS",
            "phoneSysVNum": "18.7.8",
            "terminalId": self._terminal_id(),
        }

    async def login(self) -> None:
        params = self._login_params() | {"loginAccount": self._account}
        result = await _post_form("/v1/user/login/id/get", params, self.APP_KEY)
        login_id = result["loginId"]

        params = self._login_params() | {
            "loginAccount": self._account,
            "password": self._password_hash(login_id),
            "iampwd": self._iam_password_hash(login_id),
        }
        result = await _post_form("/v1/user/login/new", params, self.APP_KEY)

        self.session_id = result["sessionId"]
        key, key_iv = app_key_and_iv(self.APP_KEY)
        self.data_key = aes_cbc_decrypt(key, key_iv, result["accessToken"])
        self.data_iv = aes_cbc_decrypt(key, key_iv, result["randomData"])

    async def send(self, appliance_id: int, data: bytes) -> bytes:
        """Relay one command frame exactly the way the Midea Air app does."""
        assert self.data_key and self.data_iv
        params = _base_params() | {
            "applianceId": str(appliance_id),
            "funId": SEND_FUN_ID,
            "order": aes_cbc_encrypt(
                self.data_key, self.data_iv,
                encode_csv(build_cloud_packet(appliance_id, data))),
            "sessionId": self.session_id,
        }
        result = await _post_form(SEND_PATH, params, self.APP_KEY)
        reply_hex = result.get("reply")
        if not reply_hex:
            raise CloudError("Cloud accepted the command but returned no reply.")
        return parse_cloud_packet(decode_csv(
            aes_cbc_decrypt(self.data_key, self.data_iv, reply_hex)))


async def transparent_send_new(cloud, appliance_id: int, data: bytes) -> bytes:
    """Relay a command frame the way the Midea Air app does."""
    app_key = cloud._security.APP_KEY
    params = {
        "applianceId": str(appliance_id),
        "format": SEND_FORMAT,
        "funId": SEND_FUN_ID,
        "language": SEND_LANGUAGE,
        "order": aes_encrypt_string(data_key_for(cloud), encode_csv(data)),
        "sessionId": cloud._session_id,
        "src": SEND_SRC,
        # The app stamps local time, not UTC.
        "stamp": datetime.now().strftime("%Y%m%d%H%M%S"),
    }
    params["sign"] = sign_body(app_key, SEND_PATH, params)

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"https://mapp.appsmb.com{SEND_PATH}",
            data=params,
            headers={"User-Agent": "Midea OBM/5.20.0 (iPhone; iOS 18.7.8; Scale/3.00)",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
    response.raise_for_status()
    body = response.json()

    if str(body.get("errorCode", "0")) != "0":
        raise CloudError(f"Code: {body.get('errorCode')}, Message: {body.get('msg')}")

    reply_hex = (body.get("result") or {}).get("reply")
    if not reply_hex:
        raise CloudError("Cloud accepted the command but returned no reply.")

    return normalize_reply(
        decode_csv(aes_decrypt_string(data_key_for(cloud), reply_hex)))


class CloudLAN:
    """Stands in for msmart's LAN object so Device._send_command works unchanged.

    Device._send_command calls self._lan.send(frame) and expects a list of
    response frames, so routing through the cloud only requires swapping this
    one object -- refresh(), apply() and all state parsing are untouched.
    """

    token = None
    key = None
    max_connection_lifetime = None

    def __init__(self, session: "NewApiSession", device_id: int) -> None:
        self._session = session
        self._device_id = device_id

    async def send(self, data: bytes, retries: int = 3) -> list[bytes]:
        last_error = None
        for _ in range(retries):
            try:
                return [await self._session.send(self._device_id, data)]
            except (CloudError, ApiError, ValueError, httpx.HTTPError) as e:
                last_error = e
        raise ProtocolError(f"Cloud relay failed: {last_error}")


# ------------------------------------------------------------ credentials --

def keychain_get(account: str) -> Optional[str]:
    """Read the password from the OS credential store.

    keyring maps to the macOS Keychain and to Windows Credential Manager, and
    finds items created by either this code or `security add-generic-password`.
    The subprocess path is kept as a fallback for macOS boxes without keyring.
    """
    try:
        import keyring
        if password := keyring.get_password(KEYCHAIN_SERVICE, account):
            return password
    except Exception:  # noqa: BLE001 - fall through to the platform tool
        pass

    if sys.platform != "darwin":
        return None

    result = subprocess.run(
        ["security", "find-generic-password",
         "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
        capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def keychain_set(account: str, password: str) -> None:
    try:
        import keyring
        keyring.set_password(KEYCHAIN_SERVICE, account, password)
        return
    except Exception:  # noqa: BLE001
        pass

    if sys.platform != "darwin":
        raise RuntimeError(
            "Could not store the password: install keyring "
            "(.venv/bin/pip install keyring) or set MIDEA_PASSWORD.")

    subprocess.run(
        ["security", "add-generic-password", "-U",
         "-s", KEYCHAIN_SERVICE, "-a", account, "-w", password],
        check=True, capture_output=True)


async def connect_cloud(account: str, password: str,
                        use_cache: bool = True) -> "NewApiSession":
    """Return a logged-in session, reusing a cached one when possible."""
    session = NewApiSession(account, password)

    if use_cache and SESSION_CACHE.exists():
        try:
            cached = json.loads(SESSION_CACHE.read_text())
            fresh = time.time() - cached.get("time", 0) < SESSION_MAX_AGE
            if fresh and cached.get("account") == account:
                session.session_id = cached["session_id"]
                session.data_key = cached["data_key"]
                session.data_iv = cached["data_iv"]
                return session
        except (json.JSONDecodeError, KeyError, OSError):
            pass

    await session.login()
    try:
        SESSION_CACHE.write_text(json.dumps({
            "time": time.time(),
            "account": account,
            "session_id": session.session_id,
            "data_key": session.data_key,
            "data_iv": session.data_iv,
        }))
        SESSION_CACHE.chmod(0o600)
    except OSError:
        pass
    return session


def clear_session_cache() -> None:
    SESSION_CACHE.unlink(missing_ok=True)
