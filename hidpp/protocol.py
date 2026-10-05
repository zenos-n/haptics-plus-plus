"""Minimal HID++ 2.0 transport over /dev/hidrawN.

No third-party deps: raw read/write on the hidraw character device.

A Logitech Bolt/Unifying receiver exposes several hidraw nodes; exactly one of
them carries the HID++ vendor collection (report IDs 0x10 short / 0x11 long /
0x12 very-long). find_hidpp_nodes() picks it by probing rather than by guessing
the interface number, which varies between receiver models.
"""

import errno
import os
import select
import struct

SHORT = 0x10
LONG = 0x11
VERY_LONG = 0x12

REPORT_LEN = {SHORT: 7, LONG: 20, VERY_LONG: 64}

# Software id, echoed back in the response so we can match request to reply.
# Any value 1..15; 0 is reserved for notifications.
SW_ID = 0x0A

ERROR_HIDPP20 = 0xFF

RECEIVER_INDEX = 0xFF

HIDPP20_ERRORS = {
    0x00: "no error",
    0x01: "unknown",
    0x02: "invalid argument",
    0x03: "out of range",
    0x04: "hardware error",
    0x05: "logitech internal",
    0x06: "invalid feature index",
    0x07: "invalid function id",
    0x08: "busy",
    0x09: "unsupported",
}

# Features we care about. 0x19B0 is the haptic feature reported by the
# community MX Master 4 work -- treated as a hypothesis until probe.py
# confirms it is in the device's feature table.
FEAT_ROOT = 0x0000
FEAT_FEATURE_SET = 0x0001
FEAT_DEVICE_NAME = 0x0005
FEAT_HAPTIC = 0x19B0


class HidppError(Exception):
    """A HID++ 2.0 error response (report id 0xFF)."""

    def __init__(self, code, feature_index, function_id):
        self.code = code
        self.feature_index = feature_index
        self.function_id = function_id
        super().__init__(
            f"HID++ error 0x{code:02X} ({HIDPP20_ERRORS.get(code, 'unknown')}) "
            f"on feature index 0x{feature_index:02X} function 0x{function_id:X}"
        )


class Timeout(Exception):
    pass


def _sysfs_uevent(hidraw_name):
    path = f"/sys/class/hidraw/{hidraw_name}/device/uevent"
    try:
        with open(path) as fh:
            return dict(
                line.strip().split("=", 1)
                for line in fh
                if "=" in line
            )
    except OSError:
        return {}



def _report_ids(hidraw_name):
    """Parse a node's HID report descriptor -> {(report_id, usage_page)}.

    /sys/.../report_descriptor is world-readable, so this works without root
    and lets us pick the HID++ node deterministically instead of guessing by
    interface number. A Logitech receiver exposes several nodes and only one
    carries the vendor collection; the others reject a 20-byte 0x11 write at
    the kernel before any HID++ traffic happens.
    """
    path = f"/sys/class/hidraw/{hidraw_name}/device/report_descriptor"
    try:
        with open(path, "rb") as fh:
            desc = fh.read()
    except OSError:
        return set()

    found = set()
    page = None
    report_id = None
    i = 0
    while i < len(desc):
        prefix = desc[i]
        size = prefix & 0x03
        size = 4 if size == 3 else size
        tag = prefix & 0xFC
        value = int.from_bytes(desc[i + 1:i + 1 + size], "little") if size else 0
        if tag == 0x04:      # Usage Page (global)
            page = value
        elif tag == 0x84:    # Report ID (global)
            report_id = value
        elif tag in (0x80, 0x90, 0xB0) and report_id is not None:
            found.add((report_id, page))
        i += 1 + size
    return found


def is_hidpp_node(hidraw_name):
    """True if the node exposes the HID++ vendor collection (0xFF00, 0x10/0x11)."""
    ids = _report_ids(hidraw_name)
    pages = {page for rid, page in ids if rid in (SHORT, LONG, VERY_LONG)}
    return 0xFF00 in pages


def find_hidpp_nodes(vendor=0x046D, strict=True):
    """Return [(path, hid_name)] for Logitech nodes that speak HID++.

    With strict=False every Logitech node is returned, which is only useful for
    diagnosing a receiver whose descriptor we failed to parse.
    """
    nodes = []
    for name in os.listdir("/sys/class/hidraw"):
        info = _sysfs_uevent(name)
        parts = info.get("HID_ID", "").split(":")
        if len(parts) != 3:
            continue
        try:
            if int(parts[1], 16) != vendor:
                continue
        except ValueError:
            continue
        if strict and not is_hidpp_node(name):
            continue
        nodes.append(
            (int(name[len("hidraw"):]), f"/dev/{name}", info.get("HID_NAME", ""))
        )
    nodes.sort()
    return [(path, name) for _, path, name in nodes]


class HidppDevice:
    """One HID++ endpoint: a receiver (index 0xFF) or a device behind it (1..6)."""

    def __init__(self, path, device_index=RECEIVER_INDEX, timeout=1.0):
        self.path = path
        self.device_index = device_index
        self.timeout = timeout
        self._fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self._feature_cache = {}

    def close(self):
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    # -- raw transport ---------------------------------------------------

    def _write(self, report_id, payload):
        buf = bytes([report_id, self.device_index]) + bytes(payload)
        buf = buf.ljust(REPORT_LEN[report_id], b"\x00")
        os.write(self._fd, buf)

    def _read_matching(self, feature_index, function_id, deadline_fn):
        """Read until a reply matches our device/feature/swid, or we time out.

        Unsolicited notifications (swId 0) and traffic for other device indices
        are dropped, which is essential on a receiver shared with a keyboard.
        """
        want_fn = (function_id << 4) | SW_ID
        while True:
            remaining = deadline_fn()
            if remaining <= 0:
                raise Timeout(
                    f"no reply for feature index 0x{feature_index:02X} "
                    f"function 0x{function_id:X}"
                )
            ready, _, _ = select.select([self._fd], [], [], remaining)
            if not ready:
                continue
            try:
                data = os.read(self._fd, 64)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                raise
            if len(data) < 4:
                continue
            if data[0] not in REPORT_LEN:
                continue
            if data[1] != self.device_index:
                continue
            if data[2] == ERROR_HIDPP20:
                # [id, dev, 0xFF, featIndex, funcId|swId, errCode]
                if len(data) >= 6 and data[3] == feature_index and data[4] == want_fn:
                    raise HidppError(data[5], feature_index, function_id)
                continue
            if data[2] != feature_index or data[3] != want_fn:
                continue
            return data[4:]

    def drain(self):
        """Discard any buffered reports.

        A call that timed out leaves its reply in the queue. Without dropping
        it first, the next call reads a stale reply, skips it as a mismatch,
        and tends to time out in turn -- one slow response desynchronises every
        call after it.
        """
        dropped = 0
        while True:
            ready, _, _ = select.select([self._fd], [], [], 0)
            if not ready:
                return dropped
            try:
                if not os.read(self._fd, 64):
                    return dropped
            except OSError:
                return dropped
            dropped += 1

    def call(self, feature_index, function_id, params=b"", report_id=LONG):
        """Issue a HID++ 2.0 request and return the response parameter bytes."""
        import time

        self.drain()
        header = bytes([feature_index, (function_id << 4) | SW_ID])
        self._write(report_id, header + bytes(params))
        end = time.monotonic() + self.timeout
        return self._read_matching(
            feature_index, function_id, lambda: end - time.monotonic()
        )

    # -- IRoot (0x0000) --------------------------------------------------

    def ping(self):
        """Return (major, minor) protocol version. Raises Timeout if absent."""
        resp = self.call(0x00, 0x01, b"\x00\x00\x5A")
        if resp[2] != 0x5A:
            raise Timeout("ping payload mismatch")
        return resp[0], resp[1]

    def feature_index(self, feature_id):
        """Map a feature id (e.g. 0x19B0) to its per-device index, 0 if absent."""
        if feature_id in self._feature_cache:
            return self._feature_cache[feature_id]
        resp = self.call(0x00, 0x00, struct.pack(">H", feature_id))
        index = resp[0]
        self._feature_cache[feature_id] = index
        return index

    def require_feature(self, feature_id):
        index = self.feature_index(feature_id)
        if index == 0:
            raise LookupError(f"device does not support feature 0x{feature_id:04X}")
        return index

    # -- IFeatureSet (0x0001) --------------------------------------------

    def feature_table(self):
        """Enumerate every feature the device exposes: [(index, id, flags)]."""
        fs = self.require_feature(FEAT_FEATURE_SET)
        count = self.call(fs, 0x00)[0]  # excludes IRoot at index 0
        table = [(0, FEAT_ROOT, 0)]
        for index in range(1, count + 1):
            resp = self.call(fs, 0x01, bytes([index]))
            feature_id = struct.unpack(">H", resp[0:2])[0]
            table.append((index, feature_id, resp[2]))
        return table

    # -- Device name (0x0005) --------------------------------------------

    def name(self):
        try:
            fi = self.require_feature(FEAT_DEVICE_NAME)
        except LookupError:
            return "<unknown>"
        length = self.call(fi, 0x00)[0]
        out = b""
        while len(out) < length:
            out += bytes(self.call(fi, 0x01, bytes([len(out)])))
        return out[:length].decode("utf-8", "replace")


def feature_flags(flags):
    names = []
    if flags & 0x80:
        names.append("obsolete")
    if flags & 0x40:
        names.append("hidden")
    if flags & 0x20:
        names.append("engineering")
    if flags & 0x10:
        names.append("manufacturing")
    return ",".join(names) or "-"


# Paired devices behind a receiver answer on 1..6. A directly-attached device
# (corded, or a kernel that does not split the receiver's children into their
# own HID devices) answers on 0xFF instead, so both are probed.
DEFAULT_INDICES = tuple(range(1, 7)) + (RECEIVER_INDEX,)

# Scanning uses a short timeout so that probing seven indices stays quick.
# Real traffic needs a longer one: the device can take well over a second to
# answer while an effect is playing.
DISCOVERY_TIMEOUT = 0.3
OPERATION_TIMEOUT = 2.0


def discover(path, indices=DEFAULT_INDICES, on_error=None):
    """Yield (device_index, HidppDevice) for every live device on a node.

    on_error(index, exc) is called for every index that fails. Without it the
    caller cannot tell "nothing paired" apart from "wrong node" or "denied",
    which are very different problems.
    """
    for index in indices:
        try:
            dev = HidppDevice(path, index, timeout=DISCOVERY_TIMEOUT)
        except OSError as exc:
            if on_error:
                on_error(index, exc)
            raise
        try:
            dev.ping()
        except (Timeout, HidppError, OSError) as exc:
            if on_error:
                on_error(index, exc)
            dev.close()
            continue
        # Discovery is over for this device; give it a workable timeout.
        dev.timeout = OPERATION_TIMEOUT
        yield index, dev
