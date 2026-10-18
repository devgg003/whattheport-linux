#!/usr/bin/env bash
# Build and install WhatThePort for the current user. `./install.sh --uninstall` removes it.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
SHARE="${XDG_DATA_HOME:-$HOME/.local/share}"
ICON_DIR="$SHARE/icons/hicolor/scalable/apps"
DESKTOP_FILE="$SHARE/applications/dev.whattheport.WhatThePort.desktop"
EXT_UUID="whattheport@local"
EXT_DIR="$SHARE/gnome-shell/extensions/$EXT_UUID"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

remove_python_version() {
    pkill -f "whattheport.gui.app" 2>/dev/null || true
    rm -rf "$SHARE/whattheport"
    rm -f "$BIN_DIR/whattheport" "$BIN_DIR/whattheport-tray" "$AUTOSTART/whattheport-tray.desktop"
}

if [[ "${1:-}" == "--uninstall" ]]; then
    "$BIN_DIR/wtp" quit 2>/dev/null || true
    command -v gnome-extensions >/dev/null && gnome-extensions disable "$EXT_UUID" 2>/dev/null || true
    rm -rf "$EXT_DIR"
    rm -f "$BIN_DIR/wtp" "$DESKTOP_FILE" "$AUTOSTART/dev.whattheport.WhatThePort.desktop" \
          "$ICON_DIR/whattheport.svg" "$ICON_DIR/whattheport-attention.svg"
    remove_python_version
    echo "WhatThePort removed. Settings are kept in ~/.config/whattheport."
    exit 0
fi

if ! command -v cargo >/dev/null && [[ -x "$HOME/.cargo/bin/cargo" ]]; then
    export PATH="$HOME/.cargo/bin:$PATH"
fi
if ! command -v cargo >/dev/null; then
    echo "Rust is needed to build WhatThePort. Install it with:"
    echo "  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh"
    exit 1
fi

echo "==> Building"
export CARGO_TARGET_DIR="$SRC/target"
cargo build --release --locked --manifest-path "$SRC/Cargo.toml"

echo "==> Installing"
"$BIN_DIR/wtp" quit 2>/dev/null || true
remove_python_version
mkdir -p "$BIN_DIR" "$ICON_DIR" "$(dirname "$DESKTOP_FILE")" "$EXT_DIR"
install -m 755 "$SRC/target/release/wtp" "$BIN_DIR/wtp"
# The panel menu loaded at login still launches this name.
cat > "$BIN_DIR/whattheport" <<EOF
#!/bin/sh
exec "$BIN_DIR/wtp" daemon
EOF
chmod 755 "$BIN_DIR/whattheport"
install -m 644 "$SRC/data/icons/whattheport.svg" "$SRC/data/icons/whattheport-attention.svg" "$ICON_DIR/"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=WhatThePort
Comment=Dev servers in your top bar
Exec=$BIN_DIR/wtp daemon
Icon=whattheport
Terminal=false
Categories=Development;Utility;
EOF

# Keep the panel menu on GNOME; other desktops get the tray icon instead.
if command -v gnome-extensions >/dev/null; then
    rm -rf "$EXT_DIR"
    cp -r "$SRC/gnome-extension/$EXT_UUID" "$EXT_DIR"
    gnome-extensions enable "$EXT_UUID" 2>/dev/null || true
fi

setsid "$BIN_DIR/wtp" daemon >/dev/null 2>&1 < /dev/null &

echo
echo "Installed $BIN_DIR/wtp and started the background service."
echo "  wtp            list dev servers in the terminal"
echo "  wtp --help     all commands"
if [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]]; then
    echo
    echo "GNOME only loads new or updated extensions at login: log out and back in"
    echo "once to see the WhatThePort icon in the top bar."
fi
