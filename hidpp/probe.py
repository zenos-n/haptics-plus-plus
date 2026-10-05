#!/usr/bin/env python3
"""Probe the MX Master 4 over HID++ and establish what the haptic feature does.

Nothing here is trusted from documentation -- the point is to find out.

    sudo python3 hidpp/probe.py scan          # receivers + paired devices
    sudo python3 hidpp/probe.py features      # full feature table of the mouse
    sudo python3 hidpp/probe.py haptic-info   # poke 0x19B0's info functions
    sudo python3 hidpp/probe.py isolate       # one fn at a time: which buzzes?
    sudo python3 hidpp/probe.py explore       # interactive: try everything, log it
    sudo python3 hidpp/probe.py call 2 01 3c  # raw call: fn, then hex params
    sudo python3 hidpp/probe.py buzz 3        # select waveform 3, then play it
    sudo python3 hidpp/probe.py sweep         # waveforms 1..8, 2s apart
    sudo python3 hidpp/probe.py repeat 1 6    # same waveform 6x: toggle test
    sudo python3 hidpp/probe.py repeat 1 6 stop   # ... with fn 0x03 after each

Add --node /dev/hidrawN anywhere to override HID++ node detection.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from protocol import (  # noqa: E402
    FEAT_HAPTIC,
    HidppError,
    Timeout,
    discover,
    feature_flags,
    find_hidpp_nodes,
)


# Set by --node to force a specific hidraw path, bypassing descriptor detection.
NODE_OVERRIDE = None


def receivers():
    if NODE_OVERRIDE:
        return [(NODE_OVERRIDE, "forced by --node")]
    nodes = find_hidpp_nodes()
    if not nodes:
        loose = find_hidpp_nodes(strict=False)
        if loose:
            sys.exit(
                "no Logitech node exposes the HID++ vendor collection.\n"
                "Logitech nodes present: "
                + ", ".join(p for p, _ in loose)
                + "\nForce one anyway with --node /dev/hidrawN."
            )
        sys.exit("no Logitech HID++ nodes found under /sys/class/hidraw")
    return nodes


def open_mouse():
    """Return the first paired device that exposes the haptic feature.

    Falls back to the first device whose name looks like an MX Master so the
    caller still gets a useful error message.
    """
    fallback = None
    for path, _ in receivers():
        try:
            for index, dev in discover(path):
                try:
                    if dev.feature_index(FEAT_HAPTIC) != 0:
                        return path, index, dev
                except (HidppError, Timeout):
                    pass
                if fallback is None and "MX Master" in dev.name():
                    fallback = (path, index, dev)
                else:
                    dev.close()
        except PermissionError:
            sys.exit(f"permission denied on {path}: run with sudo, or install the udev rule")
        except OSError as exc:
            print(f"  {path}: {exc}", file=sys.stderr)
    if fallback:
        return fallback
    sys.exit("no paired device found -- is the mouse powered on and connected?")


def cmd_scan():
    for path, hid_name in receivers():
        print(f"{path}  ({hid_name})")
        errors = []
        try:
            found = False
            for index, dev in discover(path, on_error=lambda i, e: errors.append((i, e))):
                found = True
                with dev:
                    try:
                        major, minor = dev.ping()
                        proto = f"HID++ {major}.{minor}"
                    except (Timeout, HidppError):
                        proto = "?"
                    haptic = dev.feature_index(FEAT_HAPTIC)
                    mark = f"haptic@0x{haptic:02X}" if haptic else "no haptic feature"
                    print(f"    device {index}: {dev.name()!r}  {proto}  {mark}")
            if not found:
                print("    (no devices responding)")
                for index, exc in errors:
                    label = "receiver" if index == 0xFF else f"device {index}"
                    print(f"      {label}: {type(exc).__name__}: {exc}")
        except PermissionError:
            print("    permission denied -- run with sudo or install the udev rule")
        except OSError as exc:
            print(f"    {exc}")


def cmd_features():
    path, index, dev = open_mouse()
    with dev:
        print(f"{dev.name()!r} on {path} device {index}\n")
        print(f"{'idx':>4}  {'feature':>8}  flags")
        for fidx, fid, flags in dev.feature_table():
            star = " <-- haptic" if fid == FEAT_HAPTIC else ""
            print(f"{fidx:>4}  0x{fid:04X}  {feature_flags(flags)}{star}")


def cmd_haptic_info():
    """Walk the low function ids of 0x19B0 read-only style and print replies.

    Logitech features conventionally put getInfo/getCapabilities at function 0,
    so functions 0..3 with no arguments are the cheap way to learn the shape of
    this one. Anything that errors is reported, not fatal.
    """
    _path, _index, dev = open_mouse()
    with dev:
        fi = dev.require_feature(FEAT_HAPTIC)
        print(f"{dev.name()!r}: feature 0x{FEAT_HAPTIC:04X} at index 0x{fi:02X}\n")
        for fn in range(0, 8):
            try:
                resp = dev.call(fi, fn)
                print(f"  fn 0x{fn:X}: {resp[:16].hex(' ')}")
            except HidppError as exc:
                print(f"  fn 0x{fn:X}: {exc.code:#04x} {exc}".split(" on ")[0])
            except Timeout:
                print(f"  fn 0x{fn:X}: timeout")


def cmd_isolate(args):
    """Fire each function one at a time so you can tell which one buzzes.

    Functions 0x00..0x04 are the whole feature (0x05+ return invalid function
    id). Function 0x02 is skipped by default because it rejects empty params;
    give it arguments with the `call` command instead.
    """
    gap = float(args[0]) if args else 2.5
    _path, _index, dev = open_mouse()
    with dev:
        fi = dev.require_feature(FEAT_HAPTIC)
        print(f"feature index 0x{fi:02X}; {gap}s between each\n")
        for fn in (0x00, 0x01, 0x03, 0x04):
            print(f"  -> fn 0x{fn:X} ... ", end="", flush=True)
            try:
                resp = dev.call(fi, fn)
                print(bytes(resp[:8]).hex(" ") or "(empty)", flush=True)
            except HidppError as exc:
                print(f"error {exc.code:#04x}", flush=True)
            except Timeout:
                print("timeout", flush=True)
            time.sleep(gap)
    print("\nwhich fn numbers did you feel?")


def _candidates():
    """(label, [(fn, params), ...]) pairs to try, cheapest hypothesis first.

    Covers the plausible shapes of this feature: a bare no-argument trigger, a
    trigger that takes the waveform inline, and a setter+trigger pair under
    several argument widths. A deliberate no-op control is mixed in so that
    vibrations the device produces on its own do not read as successes.
    """
    trials = [
        ("CONTROL - getInfo, must NOT buzz", [(0x00, b"")]),
        ("fn3, no args", [(0x03, b"")]),
        ("fn4, no args", [(0x04, b"")]),
        ("fn3 waveform inline", [(0x03, bytes([0x01]))]),
        ("fn4 waveform inline", [(0x04, bytes([0x01]))]),
        ("fn3 waveform+intensity", [(0x03, bytes([0x01, 0x3C]))]),
        ("fn2[01 3c] then fn3", [(0x02, bytes([0x01, 0x3C])), (0x03, b"")]),
        ("fn2[01 3c] then fn4", [(0x02, bytes([0x01, 0x3C])), (0x04, b"")]),
        ("fn2[01] then fn3", [(0x02, bytes([0x01])), (0x03, b"")]),
        ("fn2[01 3c 00 00] then fn3",
         [(0x02, bytes([0x01, 0x3C, 0x00, 0x00])), (0x03, b"")]),
        ("fn2[01 ff] then fn3", [(0x02, bytes([0x01, 0xFF])), (0x03, b"")]),
        ("CONTROL - getState, must NOT buzz", [(0x01, b"")]),
    ]
    # Then walk waveform ids through whichever trigger shape looks best.
    for wf in range(1, WAVEFORM_COUNT + 1):
        trials.append((f"fn2[{wf:02x} 3c] then fn4",
                       [(0x02, bytes([wf, 0x3C])), (0x04, b"")]))
    return trials


def cmd_explore(args):
    """Interactive: run each candidate, ask whether it buzzed, log everything.

    One privileged run replaces a dozen guess-and-report cycles.
    """
    log_path = args[0] if args else "haptics-explore.log"
    _path, _index, dev = open_mouse()
    hits = []
    with dev, open(log_path, "w") as log:
        fi = dev.require_feature(FEAT_HAPTIC)

        def record(text):
            print(text)
            log.write(text + "\n")
            log.flush()

        record(f"feature 0x{FEAT_HAPTIC:04X} at index 0x{fi:02X}")
        for fn in (0x00, 0x01):
            try:
                record(f"state fn 0x{fn:X}: {bytes(dev.call(fi, fn)[:8]).hex(' ')}")
            except (HidppError, Timeout) as exc:
                record(f"state fn 0x{fn:X}: {exc}")

        record("\nAfter each line, answer y (buzzed) / n (nothing) / q (quit).")
        record("Keep your hand ON the mouse but do not move or click it.\n")

        for label, steps in _candidates():
            results = []
            for fn, params in steps:
                results.append(f"fn{fn:X}({params.hex(' ') or '-'})="
                               f"{_try(dev, fi, fn, params)}")
            detail = " ".join(results)
            try:
                answer = input(f"  {label}\n    {detail}\n    buzz? [y/N/q] ")
            except EOFError:
                answer = "q"
            answer = answer.strip().lower()
            if answer == "q":
                record(f"{label}: {detail} -> aborted")
                break
            buzzed = answer.startswith("y")
            record(f"{label}: {detail} -> {'BUZZ' if buzzed else 'silent'}")
            if buzzed:
                hits.append(label)

        record("\n--- summary ---")
        if hits:
            record("buzzed: " + ", ".join(hits))
            if any(h.startswith("CONTROL") for h in hits):
                record("WARNING: a control buzzed, so the device is producing "
                       "haptics on its own; treat the other hits as suspect.")
        else:
            record("nothing buzzed -- this feature may not be the trigger, "
                   "or it needs enabling first (see feature 0x19C0).")
    print(f"\nwritten to {log_path}")


def cmd_call(args):
    """Raw call: `call <fn> [hex bytes]`, e.g. call 2 01 3c"""
    if not args:
        sys.exit("usage: call <fn> [hex bytes...]")
    fn = int(args[0], 0)
    params = bytes(int(b, 16) for b in args[1:])
    _path, _index, dev = open_mouse()
    with dev:
        fi = dev.require_feature(FEAT_HAPTIC)
        print(f"fn 0x{fn:X} params {params.hex(' ') or '(none)'}")
        try:
            resp = dev.call(fi, fn, params)
            print(f"  -> {bytes(resp[:16]).hex(' ')}")
        except HidppError as exc:
            print(f"  -> {exc}")
        except Timeout:
            print("  -> timeout")


# Established empirically with `explore` on an MX Master 4: fn 0x02 arms the
# effect with (waveform, intensity) and fn 0x04 fires it. A bare fn 0x04 with
# no preceding arm does nothing, and fn 0x03 does nothing in either position.
# Byte 4 of getInfo (0x08) is read as the waveform count.
SET_FN = 0x02
PLAY_FN = 0x04
DEFAULT_INTENSITY = 0x3C
WAVEFORM_COUNT = 8


def _try(dev, fi, fn, params=b""):
    try:
        dev.call(fi, fn, params)
        return "ok"
    except HidppError as exc:
        return f"err {exc.code:#04x}"
    except Timeout:
        return "timeout"


def _play(dev, fi, waveform, intensity, set_fn=SET_FN, play_fn=PLAY_FN):
    """Select a waveform, then trigger it.

    The trigger is attempted even when the setter rejects its arguments: the
    argument layout for the setter is inferred, but the trigger takes no
    arguments at all, so a bad guess on the former must not suppress the
    latter.
    """
    set_status = _try(dev, fi, set_fn, bytes([waveform, intensity]))
    # The device needs a moment between arming and firing; back to back the
    # second command lands while it is still busy and comes back 0x05.
    time.sleep(0.05)
    play_status = _try(dev, fi, play_fn)
    return f"set(0x{set_fn:X})={set_status} play(0x{play_fn:X})={play_status}"


def cmd_buzz(args):
    waveform = int(args[0], 0) if args else 1
    intensity = int(args[1], 0) if len(args) > 1 else DEFAULT_INTENSITY
    _path, _index, dev = open_mouse()
    with dev:
        fi = dev.require_feature(FEAT_HAPTIC)
        print(f"waveform {waveform} intensity {intensity} "
              f"(feature index 0x{fi:02X}): ", end="")
        print(_play(dev, fi, waveform, intensity))


def cmd_repeat(args):
    """Fire ONE waveform repeatedly: `repeat <waveform> [count] [stop]`.

    A sweep that buzzes on alternate iterations cannot distinguish "odd
    waveform ids are valid" from "the trigger toggles". Holding the waveform
    constant separates them: if this alternates too, the trigger is a toggle
    and the waveform byte is irrelevant.

    With `stop`, fn 0x03 is sent after each fire on the theory that it ends the
    effect; if that makes every iteration buzz, 0x03 is the missing stop call.
    """
    waveform = int(args[0], 0) if args else 1
    count = int(args[1], 0) if len(args) > 1 else 6
    use_stop = "stop" in args

    _path, _index, dev = open_mouse()
    with dev:
        fi = dev.require_feature(FEAT_HAPTIC)
        print(f"waveform {waveform} fired {count}x, 2s apart"
              f"{', fn 0x03 after each' if use_stop else ''}\n")
        for n in range(1, count + 1):
            status = _play(dev, fi, waveform, DEFAULT_INTENSITY)
            if use_stop:
                time.sleep(0.15)
                status += f" stop(0x3)={_try(dev, fi, 0x03)}"
            print(f"  attempt {n}: {status}", flush=True)
            time.sleep(2.0)
    print("\nevery attempt buzzed, or alternating?")


def cmd_sweep(args):
    intensity = int(args[0], 0) if args else DEFAULT_INTENSITY
    _path, _index, dev = open_mouse()
    with dev:
        fi = dev.require_feature(FEAT_HAPTIC)
        print(f"waveforms 1..{WAVEFORM_COUNT} at intensity {intensity}, "
              "2s apart -- note which ones you feel\n")
        for waveform in range(1, WAVEFORM_COUNT + 1):
            status = _play(dev, fi, waveform, intensity)
            print(f"  waveform {waveform}: {status}", flush=True)
            # Long enough for the effect to finish before the next arm.
            time.sleep(2.0)


COMMANDS = {
    "scan": lambda a: cmd_scan(),
    "features": lambda a: cmd_features(),
    "haptic-info": lambda a: cmd_haptic_info(),
    "isolate": cmd_isolate,
    "explore": cmd_explore,
    "call": cmd_call,
    "buzz": cmd_buzz,
    "repeat": cmd_repeat,
    "sweep": cmd_sweep,
}


def main():
    global NODE_OVERRIDE
    argv = sys.argv[1:]
    if "--node" in argv:
        i = argv.index("--node")
        try:
            value = argv[i + 1]
        except IndexError:
            sys.exit("--node needs a path or number, e.g. --node 5")
        del argv[i:i + 2]
        NODE_OVERRIDE = value if value.startswith("/") else f"/dev/hidraw{value}"

    if not argv or argv[0] not in COMMANDS:
        sys.exit(__doc__)
    COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    main()
