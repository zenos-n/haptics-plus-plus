#!/usr/bin/env bash
# Install the GNOME extension into the user extension directory and compile its
# schema. Does not touch the system; the daemon is run by hand (or via the
# user systemd unit) so you can watch its output while calibrating.
set -euo pipefail

UUID="hapticspp@doromiert"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/extension"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$UUID"

mkdir -p "$DEST"
cp -r "$SRC/." "$DEST/"

# glib-compile-schemas lives in glib.dev, which is not on PATH on a plain NixOS
# install, so fall back to whatever is in the store.
COMPILE="$(command -v glib-compile-schemas || true)"
if [[ -z "$COMPILE" ]]; then
    COMPILE="$(find /nix/store -maxdepth 3 -name glib-compile-schemas -type f 2>/dev/null | head -1)"
fi
if [[ -z "$COMPILE" ]]; then
    echo "glib-compile-schemas not found; try: nix-shell -p glib --run ./install.sh" >&2
    exit 1
fi
"$COMPILE" "$DEST/schemas"

echo "installed to $DEST"
echo
echo "next:"
echo "  1. log out and back in   (Wayland cannot reload the shell in place)"
echo "  2. gnome-extensions enable $UUID"
echo "  3. start the daemon:  python3 daemon/hapticsd.py"
echo "  4. hover a link, press <Super><Alt>h to calibrate"
