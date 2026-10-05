#!/usr/bin/env python3
"""Haptics daemon: owns the hidraw handle, takes buzz requests over a socket.

The GNOME extension runs inside gnome-shell and must not touch /dev/hidraw
itself, so it connects to $XDG_RUNTIME_DIR/hapticspp.sock and writes one
newline-terminated command per event:

    buzz            fire the default waveform
    buzz <n>        fire waveform n
    ping            -> "ok"

Rate limiting lives here rather than in the extension: the cursor can change
shape several times in a single frame while a page repaints, and the mouse
will queue or drop overlapping haptic commands unpredictably.
"""

import argparse
import os
import socket
import sys
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hidpp")
)

from protocol import (  # noqa: E402
    FEAT_HAPTIC,
    HidppError,
    Timeout,
    discover,
    find_hidpp_nodes,
)

# Established empirically against the MX Master 4 (see README): fn 0x02 arms
# the effect with (waveform, intensity), fn 0x04 fires it. Firing without
# arming first is silent, so both calls are always sent.
#
# Whether 0x04 is a one-shot or a start/stop toggle is not settled. Sending
# 0x03 after each fire is correct either way: as a stop it is required, and if
# 0x04 really is a one-shot then 0x03 has no observable effect. Without it a
# toggle would latch on at the first buzz and stay on.
SET_FN = 0x02
PLAY_FN = 0x04
STOP_FN = 0x03

DEFAULT_SOCKET = os.path.join(
    os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hapticspp.sock"
)


def open_mouse():
    for path, _ in find_hidpp_nodes():
        try:
            for _index, dev in discover(path):
                try:
                    if dev.feature_index(FEAT_HAPTIC) != 0:
                        return dev
                except (HidppError, Timeout):
                    pass
                dev.close()
        except OSError:
            continue
    return None


class Haptics:
    """Wraps the device, reconnecting when the mouse sleeps or roams away."""

    def __init__(self, waveform, intensity, min_interval, stop_delay=0.15):
        self.waveform = waveform
        self.intensity = intensity
        self.min_interval = min_interval
        self.stop_delay = stop_delay
        self._dev = None
        self._feature_index = 0
        self._last_fire = 0.0

    def _ensure(self):
        if self._dev is not None:
            return True
        dev = open_mouse()
        if dev is None:
            return False
        self._dev = dev
        self._feature_index = dev.feature_index(FEAT_HAPTIC)
        return True

    def _drop(self):
        if self._dev is not None:
            self._dev.close()
            self._dev = None

    def buzz(self, waveform=None):
        now = time.monotonic()
        if now - self._last_fire < self.min_interval:
            return "throttled"
        self._ensure()
        dev = self._dev
        if dev is None:
            return "no device"
        self._last_fire = now
        wf = self.waveform if waveform is None else waveform
        try:
            # Arm, then fire. Function 0x01 is a getter and 0x03 does nothing;
            # only this pairing produces a vibration.
            dev.call(self._feature_index, SET_FN, bytes([wf, self.intensity]))
            time.sleep(0.05)
            dev.call(self._feature_index, PLAY_FN)
            if self.stop_delay >= 0:
                time.sleep(self.stop_delay)
                dev.call(self._feature_index, STOP_FN)
            return "ok"
        except HidppError as exc:
            return f"error {exc.code:#04x}"
        except Timeout:
            # The mouse went to sleep mid-command; reconnect on the next buzz.
            self._drop()
            return "timeout"
        except OSError as exc:
            self._drop()
            return f"io {exc.errno}"


def handle(conn, haptics):
    with conn:
        conn.settimeout(30)
        buf = b""
        while True:
            try:
                chunk = conn.recv(256)
            except socket.timeout:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                reply = dispatch(line.decode("ascii", "replace").strip(), haptics)
                try:
                    conn.sendall((reply + "\n").encode())
                except OSError:
                    return


def dispatch(line, haptics):
    if not line:
        return "ok"
    parts = line.split()
    if parts[0] == "ping":
        return "ok"
    if parts[0] == "buzz":
        waveform = None
        if len(parts) > 1:
            try:
                waveform = int(parts[1], 0)
            except ValueError:
                return "bad waveform"
        return haptics.buzz(waveform)
    return "unknown command"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--socket", default=DEFAULT_SOCKET)
    ap.add_argument("--waveform", type=lambda v: int(v, 0), default=1,
                    help="default waveform id (find a good one with probe.py sweep)")
    ap.add_argument("--intensity", type=lambda v: int(v, 0), default=0x3C,
                    help="intensity passed to the waveform setter")
    ap.add_argument("--min-interval", type=float, default=0.06,
                    help="seconds between haptic pulses")
    ap.add_argument("--stop-delay", type=float, default=0.15,
                    help="seconds after firing before sending the stop; "
                         "negative disables the stop entirely")
    args = ap.parse_args()

    if os.path.exists(args.socket):
        os.unlink(args.socket)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(args.socket)
    os.chmod(args.socket, 0o600)
    server.listen(4)
    print(f"listening on {args.socket}", flush=True)

    haptics = Haptics(args.waveform, args.intensity, args.min_interval,
                      args.stop_delay)
    if haptics._ensure():
        print("mouse found", flush=True)
    else:
        print("mouse not found yet; will retry on first buzz", flush=True)

    try:
        while True:
            conn, _ = server.accept()
            handle(conn, haptics)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        os.unlink(args.socket)


if __name__ == "__main__":
    main()
