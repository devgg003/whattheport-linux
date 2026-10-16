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

# 4. Install autostart entry for the system tray indicator
cat << EOF > "$AUTOSTART_DIR/whattheport-tray.desktop"
[Desktop Entry]
Name=WhatThePort Tray
Comment=WhatThePort system tray service
Exec=$BIN_DIR/whattheport-tray
Icon=$APP_DIR/whattheport/gui/icons/wtp-normal.svg
Terminal=false
Type=Application
Categories=Utility;Development;
X-GNOME-Autostart-enabled=true
EOF

echo "==> Binaries installed: $BIN_DIR/whattheport, $BIN_DIR/whattheport-tray, $BIN_DIR/wtp"
echo "==> Autostart entry installed: $AUTOSTART_DIR/whattheport-tray.desktop"

# 5. Launch or restart tray service if in a desktop session
if [ -n "$DISPLAY" ] || [ -n "$WAYLAND_DISPLAY" ]; then
    pkill -f "whattheport.gui" 2>/dev/null || true
    nohup "$BIN_DIR/whattheport-tray" >/dev/null 2>&1 &
    disown 2>/dev/null || true
    echo "==> System tray daemon started!"
fi

echo "==> Installation complete!"
echo "Run 'wtp' in your terminal or launch WhatThePort from your apps menu."
