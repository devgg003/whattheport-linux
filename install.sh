#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/whattheport"
DESKTOP_DIR="$HOME/.local/share/applications"
AUTOSTART_DIR="$HOME/.config/autostart"

echo "==> Installing WhatThePort for Linux..."

# 1. Install app files
mkdir -p "$APP_DIR" "$BIN_DIR" "$DESKTOP_DIR" "$AUTOSTART_DIR"
cp -r "$SCRIPT_DIR/whattheport" "$APP_DIR/"

# 2. Create launcher wrappers in ~/.local/bin/
cat << 'EOF' > "$BIN_DIR/whattheport"
#!/usr/bin/env bash
PYTHONPATH="$HOME/.local/share/whattheport" exec python3 -m whattheport.gui.app "$@"
EOF
chmod +x "$BIN_DIR/whattheport"

cat << 'EOF' > "$BIN_DIR/whattheport-tray"
#!/usr/bin/env bash
PYTHONPATH="$HOME/.local/share/whattheport" exec python3 -m whattheport.gui.app --tray "$@"
EOF
chmod +x "$BIN_DIR/whattheport-tray"

cat << 'EOF' > "$BIN_DIR/wtp"
#!/usr/bin/env bash
PYTHONPATH="$HOME/.local/share/whattheport" exec python3 -m whattheport.tui.wtp "$@"
EOF
chmod +x "$BIN_DIR/wtp"

# 3. Install desktop launcher
cat << EOF > "$DESKTOP_DIR/dev.whattheport.WhatThePort.desktop"
[Desktop Entry]
Name=WhatThePort
Comment=Dev servers in your top bar
Exec=$BIN_DIR/whattheport
Icon=$APP_DIR/whattheport/gui/icons/wtp-normal.svg
Terminal=false
Type=Application
Categories=Development;Utility;
StartupNotify=true
EOF

# The top-bar icon is created by the app itself and removed on quit.
# Do not leave a login autostart entry from older installs.
rm -f "$AUTOSTART_DIR/whattheport-tray.desktop"

# 4b. Optional GNOME panel indicator, used when the StatusNotifier icon is absent.
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/whattheport@local"
mkdir -p "$EXT_DIR"
cp "$SCRIPT_DIR/gnome-extension/whattheport@local/extension.js" \
   "$SCRIPT_DIR/gnome-extension/whattheport@local/stylesheet.css" \
   "$SCRIPT_DIR/gnome-extension/whattheport@local/metadata.json" \
   "$EXT_DIR/"
if command -v gnome-extensions >/dev/null 2>&1; then
    gnome-extensions enable whattheport@local 2>/dev/null || true
fi

echo "==> Binaries installed: $BIN_DIR/whattheport, $BIN_DIR/whattheport-tray, $BIN_DIR/wtp"
echo "==> Panel indicator installed: $EXT_DIR"
echo "==> Open WhatThePort from the app menu. The top-bar icon appears with the app and leaves when it quits."

echo "==> Installation complete!"
echo "Run 'wtp' in your terminal or launch WhatThePort from your apps menu."
