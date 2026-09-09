# ac-deck

Control a Midea **MAP14AS1TWT-C** Duo portable air conditioner from an Elgato
Stream Deck.

```bash
./bin/ac-on.sh        # power=ON  mode=COOL  target=70F  fan=AUTO  room=76F
./bin/ac-off.sh
./bin/ac-status.sh
```

~0.6s per press with a cached session.

## Stream Deck plugin (recommended)

```bash
./install-plugin.sh
```

Adds a **Midea AC** category with four actions. Keys render live state,
refreshed every 20s and immediately after any press:

| Action | Key shows | Press does |
|---|---|---|
| **AC Power** | `ON` / `OFF` + room temp, icon changes colour | toggles power |
| **Temperature Up** | current setpoint | +1F (turns on if off) |
| **Temperature Down** | current setpoint | -1F |
| **AC Status** | room temp, setpoint, mode | forces a refresh |

One shared poller drives every key, so five keys still cost one cloud request
per interval.

Presses repaint the key **immediately** from local state and send the command
behind it, so the ~0.6s cloud round trip is not in the visual path. Rapid
temperature presses are coalesced: tapping Up four times repaints four times
but sends a single command 0.6s after the last press, instead of four
commands. `apply()` also reads the setpoint back out of the SetState reply, so
no confirming `refresh()` is needed.

The plugin is installed as a symlink to this repo, so edits take effect on the
next Stream Deck restart. Logs go to `streamdeck-plugin/plugin.log`.

**First launch may show a Keychain prompt** ("Stream Deck wants to use your
confidential information"). Choose *Always Allow* -- the plugin reads the
Midea password from the Keychain rather than storing it.

## Windows

The plugin runs on Windows 10+ as well, but it is **not** plug and play --
exporting a Stream Deck profile is not enough, because a profile only stores
key layout and settings referencing action UUIDs. If the plugin is not
installed, those keys come up blank. The Python project has to exist on the
Windows machine too.

```powershell
git clone https://github.com/jasonzzeng/midea-streamdeck
cd midea-streamdeck
powershell -ExecutionPolicy Bypass -File .\setup-windows.ps1 -Account you@example.com
powershell -ExecutionPolicy Bypass -File .\install-plugin.ps1
```

That creates the venv, installs dependencies, discovers the AC, stores the
password in **Windows Credential Manager** (via `keyring`, the same code path
that uses the macOS Keychain), and links the plugin. Then export your profile
on the Mac (right-click the profile → Export) and import it on Windows -- with
the plugin present, the keys resolve.

Not portable: `build-apps.sh` and the `.app` launchers are macOS-only. You do
not need them -- the plugin supersedes them on both platforms.

**Using one Stream Deck across both machines via a USB KVM** works, with the
caveat that each machine needs its own full install (Stream Deck software,
this plugin, the venv, credentials). Only the machine currently holding the
USB sees the device, and each keeps its own profile store, so a profile edited
on one side does not follow the cable.

## Stream Deck without the plugin

```bash
./build-apps.sh
```

Builds `streamdeck/AC On.app`, `AC Off.app`, `AC Toggle.app`. Drag
**System → Open** onto a key and point it at one. They run silently, but a
plain `.app` cannot draw on the key, so there is no live readout.

## CLI

```bash
.venv/bin/python ac.py on|off|toggle|status
.venv/bin/python ac.py temp 68        # Fahrenheit (values <=45 read as Celsius)
.venv/bin/python ac.py mode cool|heat|dry|fan_only|auto
.venv/bin/python ac.py fan auto|low|medium|high|max|silent
```

`on` and `toggle` apply the `defaults` block from `config.json` whenever the
unit is coming up from off, so one press lands you in a known state.

The unit's confirmation beep is controlled by `"beep"` in the `defaults`
block. Turn it on for audible confirmation that a press actually reached the
AC -- useful precisely because the transport is cloud-based.

## Setup on a new machine

```bash
./setup.sh 192.168.68.119 --account you@example.com
```

Tries LAN first, falls back to the cloud relay, stores the password in the
macOS Keychain (service `ac-deck-midea`), and writes `config.json`.

---

## How this actually works, and why it isn't msmart-ng

**This unit has no LAN token, so every public Midea library fails on it.**

Midea's V3 LAN protocol needs a token+key that only the cloud can issue. For
this AC the cloud answers `getToken` with `9999 system error` -- for *any*
account, *any* app identity, and *any* udpid derivation. The decisive evidence:
a packet capture of the **Midea Air app itself** shows the app making the same
`getToken` call and getting the same `9999`. Midea's own software cannot get a
LAN token for this hardware. It falls back to cloud control, and so do we.

Things that were ruled out, so nobody repeats them:

| Attempt | Result |
|---|---|
| `msmart-ng` LAN + token | `9999` on every udpid derivation |
| `midea-local`, 7 cloud providers | logs in, returns no keys |
| `midea-local` preset helper account | no keys |
| Universal `DEFAULT_KEYS[99]` over LAN | rejected by the AC |
| Re-provisioning via **NetHome Plus** | still no token |
| Legacy `/v1/appliance/transparent/send` | `3176`, never reaches the unit |
| Matter | not advertised; only port 6444 open |

### The working path

Everything below came from a capture of Midea Air 5.20.0 and is implemented in
`midea_cloud.py`. Public libraries implement none of it.

1. **Login** via `/v1/user/login/new` (not `/v1/user/login`) as app id `1117`
   ("Midea Air"), with `iampwd`, `encryptVersion`, `terminalId`. The response
   carries an extra `randomData` field alongside `accessToken`.
2. **Session crypto**: `sha256(app_key)` splits into `key = [:16]` and
   `iv = [16:32]`; AES-**CBC**-decrypting `accessToken` and `randomData` with
   those yields the per-session data key and data IV. The legacy flow uses
   AES-ECB with `md5(app_key)`, and `/new` rejects it with `1011`.
3. **Send** to `/v1/appliance/transparent/send/new` with `funId=FC02`,
   `src=17`, `format=2` -- and *no* `appId`/`clientType`/`reqId`. The legacy
   endpoint with `funId=0000` returns `3176` forever.
4. **Payload**: a `0x5A5A` packet whose inner command frame is **plaintext**,
   message type `0x0110` (the LAN protocol uses `0x0111` with an AES-encrypted
   payload). Replies come back as `0x0100`, also in the clear.
5. **Signature**: `sha256(path + sorted_urlencoded_params + app_key)`.

The command frames themselves are still built and parsed by `msmart-ng` --
only the transport is ours. `CloudLAN` substitutes for `device._lan`, so
`refresh()`, `apply()` and all of msmart's state decoding work untouched.

### Files

| File | Purpose |
|---|---|
| `ac.py` | CLI; picks transport from `config.json` |
| `midea_cloud.py` | The cloud transport: login, crypto, packet build/parse |
| `setup.py` | Discovery, LAN attempt, cloud fallback, Keychain |
| `diagnose.py` | udpid sweep + raw cloud record, for when things break |
| `streamdeck-plugin/` | The Stream Deck plugin (manifest, `plugin.py`, icons) |
| `install-plugin.sh` | Symlinks the plugin and restarts Stream Deck |
| `bin/*.sh` | Thin wrappers |
| `build-apps.sh` | Generates the silent `.app` launchers |

## Can someone else use this?

Yes, with caveats worth stating plainly.

**Should work as-is** for any Midea AC that (a) is set up in the **Midea Air**
app and (b) returns `9999` from `getToken`. The protocol work is not specific
to one unit -- `setup.sh` discovers the device, tries LAN, and falls back to
the cloud automatically.

**Will need changing** if your unit is in a different app. The app identity is
hardcoded to Midea Air (`appId 1117`, and its key). MSmartHome and Meiju use a
different host and an HMAC signature, so `NewApiSession` would need a sibling.

**Try the normal route first.** If [`midea-ac-py`](https://github.com/mill1000/midea-ac-py)
or [`midea_ac_lan`](https://github.com/wuwentao/midea_ac_lan) can get a token
for your device, use them -- they are maintained, and LAN control is faster and
survives Midea changing their API. This repo is for hardware where that path
does not exist.

**macOS only** as written: the Keychain calls (`security`) and the `.app`
launchers are platform-specific. The transport in `midea_cloud.py` is portable;
swap the credential store and it runs anywhere.

**Nothing secret is committed.** `config.json` and `.session.json` are
gitignored, and the password lives in the Keychain. `config.example.json` shows
the shape. Your `device_id`, IP and account never enter the repo.

### Notes

- The password lives in the **macOS Keychain**, never in `config.json`.
  `.session.json` caches the session for 6h (mode 600) so a press is one request.
- Control is **cloud-dependent**: no internet, no buttons. That is not a choice
  we made -- this unit provides no local path. If Midea changes the `/new`
  endpoint this breaks, and the fix is another capture.
- Give the AC a static DHCP lease. The IP is only used for discovery, but
  `setup.sh` wants to find it.
- The AC does **not** answer ICMP -- a ping sweep will miss it. Scan port 6444.
