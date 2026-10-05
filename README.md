# haptics++

Buzz the MX Master 4 when the pointer turns into the hand/click cursor, on
GNOME Wayland.

## Why this is buildable on Wayland

The usual advice is that you cannot observe another window's cursor shape under
Wayland. That is true for a *client*, but a GNOME Shell extension runs inside
mutter, which is the thing that owns the cursor. `Meta.CursorTracker` emits
`cursor-changed` and hands you the sprite, so no XFixes polling and no
compositor patching is needed.

Verified present in the mutter 49.4 actually running on this machine:

- `meta_backend_get_cursor_tracker`
- `cursor-changed`
- `meta_cursor_tracker_get_sprite` / `get_hot` / `get_scale`

## Pieces

| path | what |
|---|---|
| `hidpp/protocol.py` | HID++ 2.0 over `/dev/hidrawN`, no third-party deps |
| `hidpp/probe.py` | CLI to find the mouse and work out what the haptic feature does |
| `daemon/hapticsd.py` | Owns the hidraw handle, takes `buzz` over a unix socket, rate limits |
| `extension/` | GNOME 49 extension: watches `cursor-changed`, pings the daemon |
| `nix/99-hidpp-haptics.rules` | udev rule so none of this needs root |

The extension does not touch `/dev/hidraw` itself — gnome-shell should not hold
a raw device handle, and a wedged write there would stall the compositor.

## Feature 0x19B0, as far as it has been mapped

Solaar reports this feature as `unknown:19B0`, so everything below was
established by probing an MX Master 4 (device index 2 behind a Bolt receiver,
HID++ 4.5) rather than from documentation.

| fn | behaviour |
|---|---|
| `0x00` | getInfo -> `00 01 00 3c 08 00 7f ff` |
| `0x01` | getter -> `01 3c`, i.e. bytes 1 and 3 of getInfo |
| `0x02` | **arm**: takes `(waveform, intensity)`; rejects empty params |
| `0x03` | accepted, no observable effect |
| `0x04` | **fire**: no arguments |
| `0x05`+ | invalid function id |

**Arming is mandatory.** `fn 0x04` on its own is silent; only
`0x02(waveform, intensity)` followed by `0x04` vibrates. Byte 4 of getInfo
(`0x08`) is read as the waveform count, and `7f ff` is probably a maximum for
one of the two parameters.

Confirmed working: `fn 0x02` with `01 3c`, then `fn 0x04`. Waveform 1 fires
repeatedly and reliably.

A sweep of waveforms 1..8 buzzed on odd ids only. That has two readings which
this project has not yet separated: odd ids are the valid ones, or `fn 0x04`
is a start/stop toggle and the alternation was an artifact of iteration count.
The daemon therefore sends `fn 0x03` after each fire, which is correct under
both readings -- as a stop it is required, and against a true one-shot it does
nothing observable. `--stop-delay -1` disables it.

Still open: whether the waveform byte selects distinct effects, whether `0x3c`
is intensity or duration, and what neighbouring feature `0x19C0` is for.

## Timing

Back-to-back haptic commands fail. The device needs ~50 ms between arm and
fire, and a slow reply desynchronises every subsequent call unless the read
queue is drained first (`HidppDevice.drain`). Discovery uses a 0.3 s timeout
for speed; real traffic uses 2 s, because the device can take over a second to
answer while an effect is playing.

This matters for cursor feedback: `--min-interval` defaults to 60 ms, which is
faster than anything yet demonstrated to work reliably. If buzzes start
dropping under rapid hovering, raise it.

## Status: waveform selection is unverified

The feature id `0x19B0`, the function id `0x01`, and the "16 waveforms" claim
all come from community reports, **not** from anything confirmed on this
device. `probe.py` exists to establish them rather than assume them:

```sh
sudo python3 hidpp/probe.py scan          # is the mouse talking at all?
sudo python3 hidpp/probe.py features      # does 0x19B0 appear in its feature table?
sudo python3 hidpp/probe.py haptic-info   # what do the feature's info functions return?
sudo python3 hidpp/probe.py sweep         # fire waveforms 0x00..0x0F, one per second
```

If `features` does not list `0x19B0`, the whole haptic assumption is wrong and
the sweep will just return "invalid feature index" — that is the single most
important thing to check first.

To drop the `sudo`, add the udev rule (NixOS):

```nix
services.udev.extraRules = builtins.readFile ./nix/99-hidpp-haptics.rules;
```

## Cursor detection: calibrated, not hardcoded

Mutter gives you a `CoglTexture`, not a name like `pointer` or `hand2`, and the
sprite is whatever your cursor theme draws — so there is no correct hash to
ship. Instead the extension learns:

1. Hover something clickable (a link).
2. Press <kbd>Super</kbd>+<kbd>Alt</kbd>+<kbd>H</kbd>.
3. That cursor's signature is stored, and buzzes from then on.

<kbd>Super</kbd>+<kbd>Alt</kbd>+<kbd>J</kbd> clears the list. You can calibrate
several cursors (hand, text caret, resize handles) if you want.

A signature is `WxH+hotX,hotY`, plus an FNV-1a hash of the sprite's alpha
channel when `Cogl.Texture.get_data` is callable from GJS. Size and hotspot
alone already separate arrow from hand in every stock theme; the hash is a
best-effort upgrade for themes that reuse one geometry across shapes. Set the
`debug` key to log every signature transition to the journal:

```sh
gsettings --schemadir ~/.local/share/gnome-shell/extensions/hapticspp@doromiert/schemas \
  set org.gnome.shell.extensions.hapticspp debug true
journalctl -f -o cat /usr/bin/gnome-shell | grep haptics
```

## Install

```sh
./install.sh
# log out and back in -- Wayland cannot reload the shell in place
gnome-extensions enable hapticspp@doromiert
python3 daemon/hapticsd.py
```

## Known rough edges

- Buzzing on *every* hand-cursor transition may be too much; the daemon's
  `--min-interval` (default 60 ms) is the blunt fix, but a dwell delay in the
  extension would be better.
- The daemon reconnects when the mouse sleeps, but the first buzz after wake is
  swallowed by the reconnect.
- Device index `0xFF` is probed alongside `1..6` because on this machine the
  mouse does not enumerate as a separate child HID device behind the receiver.

## Which hidraw node

A Logitech receiver exposes several nodes and only one carries HID++. The node
is chosen by parsing `/sys/class/hidraw/hidrawN/device/report_descriptor`
(world-readable, so this needs no root) and keeping nodes that declare report
`0x10`/`0x11` on vendor usage page `0xFF00`. On this machine:

| node | collection | HID++ |
|---|---|---|
| hidraw3 | — | no |
| hidraw4 | consumer / generic desktop | no |
| **hidraw5** | **vendor 0xFF00, `0x10` 7 B + `0x11` 20 B** | **yes** |
| hidraw6 | generic desktop + button + digitizer | no |

`hidraw6` is where pointer *input* arrives, so it looks like "the mouse", but a
20-byte `0x11` write there is rejected by the kernel before any HID++ traffic
happens. Use `--node /dev/hidrawN` to override the detection.
