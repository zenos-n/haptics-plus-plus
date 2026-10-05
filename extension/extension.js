import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

/* Cursor sprites are theme-specific, so there is nothing sensible to hardcode.
 * The user hovers a clickable element and presses the calibrate shortcut; we
 * record whatever signature the cursor has at that moment and buzz on it from
 * then on.
 *
 * A signature is "WxH+hotX,hotY" plus, when Cogl lets us read the pixels, a
 * hash of the sprite. Size and hotspot alone already separate the arrow from
 * the hand in every stock theme; the pixel hash mainly helps themes that reuse
 * one geometry for several shapes. */

const PIXEL_HASH_UNKNOWN = 0;
const PIXEL_HASH_OK = 1;
const PIXEL_HASH_UNAVAILABLE = 2;

export default class HapticsPlusPlusExtension extends Extension {
    enable() {
        this._settings = this.getSettings();
        this._tracker = global.backend.get_cursor_tracker();
        this._lastSignature = null;
        this._pixelHashState = PIXEL_HASH_UNKNOWN;
        this._client = null;
        this._stream = null;
        this._connecting = false;
        this._pending = 0;

        this._signatures = new Set(this._settings.get_strv('pointer-signatures'));
        this._settingsChangedId = this._settings.connect(
            'changed::pointer-signatures',
            () => {
                this._signatures = new Set(
                    this._settings.get_strv('pointer-signatures'));
            });

        this._cursorChangedId = this._tracker.connect(
            'cursor-changed', () => this._onCursorChanged());

        Main.wm.addKeybinding(
            'calibrate-pointer', this._settings,
            Meta.KeyBindingFlags.NONE, Shell.ActionMode.ALL,
            () => this._calibrate());
        Main.wm.addKeybinding(
            'forget-signatures', this._settings,
            Meta.KeyBindingFlags.NONE, Shell.ActionMode.ALL,
            () => this._forget());
    }

    disable() {
        if (this._cursorChangedId) {
            this._tracker.disconnect(this._cursorChangedId);
            this._cursorChangedId = null;
        }
        if (this._settingsChangedId) {
            this._settings.disconnect(this._settingsChangedId);
            this._settingsChangedId = null;
        }
        Main.wm.removeKeybinding('calibrate-pointer');
        Main.wm.removeKeybinding('forget-signatures');

        this._disconnectSocket();
        this._tracker = null;
        this._settings = null;
        this._signatures = null;
        this._lastSignature = null;
    }

    /* -- cursor identification ------------------------------------------ */

    _pixelHash(sprite) {
        if (this._pixelHashState === PIXEL_HASH_UNAVAILABLE)
            return null;
        try {
            const width = sprite.get_width();
            const height = sprite.get_height();
            const rowstride = width * 4;
            const buffer = new Uint8Array(rowstride * height);
            // Cogl.PixelFormat.RGBA_8888_PRE == 0x2103 in the ABI we build
            // against; passing the numeric value avoids importing Cogl just
            // for one enum, which some shell versions do not re-export.
            const written = sprite.get_data(0x2103, rowstride, buffer);
            if (!written) {
                this._pixelHashState = PIXEL_HASH_UNAVAILABLE;
                return null;
            }
            // FNV-1a over every fourth byte: cheap, and alpha coverage alone
            // distinguishes cursor shapes reliably.
            let hash = 0x811c9dc5;
            for (let i = 3; i < buffer.length; i += 4) {
                hash ^= buffer[i];
                hash = Math.imul(hash, 0x01000193) >>> 0;
            }
            this._pixelHashState = PIXEL_HASH_OK;
            return hash.toString(16);
        } catch (e) {
            this._pixelHashState = PIXEL_HASH_UNAVAILABLE;
            return null;
        }
    }

    _signature() {
        const sprite = this._tracker.get_sprite();
        if (!sprite)
            return null;

        let hotX = 0;
        let hotY = 0;
        try {
            [hotX, hotY] = this._tracker.get_hot();
        } catch (e) {
            // get_hot is introspectable but has thrown on some builds; the
            // geometry alone is still a usable signature.
        }

        const base = `${sprite.get_width()}x${sprite.get_height()}` +
            `+${hotX},${hotY}`;
        const hash = this._pixelHash(sprite);
        return hash === null ? base : `${base}#${hash}`;
    }

    _onCursorChanged() {
        const signature = this._signature();
        if (signature === null || signature === this._lastSignature)
            return;

        const previous = this._lastSignature;
        this._lastSignature = signature;

        if (this._settings.get_boolean('debug'))
            console.log(`haptics++: cursor ${previous} -> ${signature}`);

        // Only buzz on the transition into a calibrated cursor, never on the
        // way out, and never for the very first cursor we ever see.
        if (previous !== null && this._signatures.has(signature))
            this._buzz();
    }

    /* -- calibration ----------------------------------------------------- */

    _calibrate() {
        const signature = this._signature();
        if (signature === null) {
            Main.notify('haptics++', 'No cursor sprite available right now.');
            return;
        }
        const stored = this._settings.get_strv('pointer-signatures');
        if (stored.includes(signature)) {
            Main.notify('haptics++', `Already buzzing on ${signature}.`);
            return;
        }
        stored.push(signature);
        this._settings.set_strv('pointer-signatures', stored);
        Main.notify('haptics++', `Will buzz on ${signature}.`);
        this._buzz();
    }

    _forget() {
        this._settings.set_strv('pointer-signatures', []);
        Main.notify('haptics++', 'Cleared all calibrated cursors.');
    }

    /* -- daemon link ------------------------------------------------------ */

    _socketPath() {
        const configured = this._settings.get_string('socket-path');
        if (configured)
            return configured;
        return GLib.build_filenamev([GLib.get_user_runtime_dir(), 'hapticspp.sock']);
    }

    _disconnectSocket() {
        if (this._connection) {
            try {
                this._connection.close(null);
            } catch (e) {
                // Already gone; nothing to do.
            }
            this._connection = null;
        }
        this._stream = null;
    }

    _buzz() {
        const waveform = this._settings.get_int('waveform');
        const command = waveform < 0 ? 'buzz\n' : `buzz ${waveform}\n`;

        if (this._stream) {
            this._send(command);
            return;
        }
        // Coalesce: a burst of cursor changes while we are still connecting
        // should not queue a burst of buzzes.
        this._pending = 1;
        this._connect(command);
    }

    _connect(command) {
        if (this._connecting)
            return;
        this._connecting = true;

        const client = new Gio.SocketClient();
        const address = Gio.UnixSocketAddress.new(this._socketPath());
        client.connect_async(address, null, (source, result) => {
            this._connecting = false;
            let connection;
            try {
                connection = client.connect_finish(result);
            } catch (e) {
                this._pending = 0;
                if (this._settings?.get_boolean('debug'))
                    console.log(`haptics++: daemon unreachable: ${e.message}`);
                return;
            }
            this._connection = connection;
            this._stream = connection.get_output_stream();
            if (this._pending) {
                this._pending = 0;
                this._send(command);
            }
        });
    }

    _send(command) {
        const stream = this._stream;
        if (!stream)
            return;
        try {
            stream.write_all_async(
                new TextEncoder().encode(command),
                GLib.PRIORITY_DEFAULT, null,
                (source, result) => {
                    try {
                        stream.write_all_finish(result);
                    } catch (e) {
                        // Daemon restarted or died; drop and reconnect next time.
                        this._disconnectSocket();
                    }
                });
        } catch (e) {
            this._disconnectSocket();
        }
    }
}
