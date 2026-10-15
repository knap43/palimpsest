#!/usr/bin/env bash
# Installs Palimpsest for the current user. Keep all four files in one folder.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.knap43.Palimpsest
UUID=palimpsest@knap43.github.io
data="${XDG_DATA_HOME:-$HOME/.local/share}"
bin="$HOME/.local/bin"

missing=()
for pkg in python-gobject gtk4 libadwaita gdk-pixbuf2; do
    pacman -Q "$pkg" &>/dev/null || missing+=("$pkg")
done
if ((${#missing[@]})); then
    echo "Missing packages. Run:  sudo pacman -S --needed ${missing[*]}" >&2
    exit 1
fi

install -Dm755 "$here/palimpsest.py" "$bin/palimpsest"
install -Dm644 "$here/extension.js"   "$data/gnome-shell/extensions/$UUID/extension.js"
install -Dm644 "$here/metadata.json"  "$data/gnome-shell/extensions/$UUID/metadata.json"

# Declare compatibility with the running GNOME Shell version.
shell_ver=$(gnome-shell --version | grep -oE '[0-9]+' | head -1)
python3 - "$data/gnome-shell/extensions/$UUID/metadata.json" "$shell_ver" <<'PY'
import json, sys
path, ver = sys.argv[1:]
meta = json.load(open(path))
meta["shell-version"] = sorted(set(meta["shell-version"] + [ver]), key=int)
json.dump(meta, open(path, "w"), indent=2)
PY

# D-Bus activation: the first copy after login starts Palimpsest in the background.
install -Dm644 /dev/stdin "$data/dbus-1/services/$APP_ID.service" <<EOF
[D-BUS Service]
Name=$APP_ID
Exec=$bin/palimpsest --gapplication-service
EOF

install -Dm644 /dev/stdin "$data/applications/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Palimpsest
Comment=Clipboard history
Exec=$bin/palimpsest
Icon=edit-paste
Terminal=false
Categories=Utility;
StartupNotify=true
EOF

gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
    --method org.freedesktop.DBus.ReloadConfig &>/dev/null || true

# Stop a running instance so the next copy or shortcut starts the new version.
if pkill -f "$bin/palimpsest"; then
    echo "Restarted the running Palimpsest."
fi

# Enable the extension (takes effect after the next login on Wayland).
python3 - "$UUID" <<'PY'
import sys
from gi.repository import Gio
s = Gio.Settings.new("org.gnome.shell")
uuid = sys.argv[1]
enabled = s.get_strv("enabled-extensions")
if uuid not in enabled:
    s.set_strv("enabled-extensions", enabled + [uuid])
s.set_boolean("disable-user-extensions", False)
PY

cat <<EOF

Installed. Log out and back in once so GNOME Shell loads the extension.

Shortcut command (Settings → Keyboard → Custom Shortcuts):
    $bin/palimpsest
EOF
