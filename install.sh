#!/usr/bin/env bash
# Install LLM Usage Widget for the current user and start it at login.
#
#   ./install.sh              install (or update) and start it now
#   ./install.sh --uninstall  stop it and remove everything this script installed
#
# Linux: copies the app to ~/.local/share/llm-usage-widget, adds an XDG autostart
#        entry and an app-menu launcher. Uses the system Python's PyGObject.
# macOS: copies the app to ~/Library/Application Support/llm-usage-widget with its
#        own virtualenv (pystray + Pillow) and registers a LaunchAgent.
#
# Settings in the per-user config directory are left in place on uninstall.
set -euo pipefail

APP=llm-usage-widget
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# Any running copy (installed or run from a checkout) holds the single-instance lock.
stop_running() {
    pkill -f 'python[0-9.]* .*usage_widget\.py' 2>/dev/null || true
    sleep 1
}

# ---------------------------------------------------------------- Linux

linux_paths() {
    DEST="${XDG_DATA_HOME:-$HOME/.local/share}/$APP"
    AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/$APP.desktop"
    LAUNCHER="${XDG_DATA_HOME:-$HOME/.local/share}/applications/$APP.desktop"
    ICON="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps/$APP.svg"
}

linux_python() {
    # Prefer the system interpreter: pyenv/conda builds rarely have PyGObject.
    local py
    for py in /usr/bin/python3 "$(command -v python3 || true)"; do
        [ -x "$py" ] || continue
        if "$py" - <<'EOF' 2>/dev/null; then echo "$py"; return; fi
import gi
gi.require_version("Gtk", "3.0")
for name in ("AyatanaAppIndicator3", "AppIndicator3"):
    try:
        gi.require_version(name, "0.1")
        break
    except ValueError:
        pass
else:
    raise SystemExit(1)
EOF
    done
    return 1
}

linux_missing_deps_hint() {
    if command -v apt-get >/dev/null; then
        echo "sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1"
    elif command -v dnf >/dev/null; then
        echo "sudo dnf install python3-gobject libappindicator-gtk3"
    elif command -v pacman >/dev/null; then
        echo "sudo pacman -S python-gobject libayatana-appindicator"
    elif command -v zypper >/dev/null; then
        echo "sudo zypper install python3-gobject typelib-1_0-AyatanaAppIndicator3-0_1"
    else
        echo "install PyGObject and libayatana-appindicator (or libappindicator) for GTK 3"
    fi
}

write_desktop_entry() {  # $1 = path, $2 = python, $3 = extra lines
    mkdir -p "$(dirname "$1")"
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Name=LLM Usage Widget
Comment=Claude and Codex usage limits in the top bar
Exec=$2 "$DEST/usage_widget.py"
Icon=$APP
Terminal=false
Categories=Utility;
$3
EOF
}

# Ubuntu's AppIndicators extension on GNOME 50 rejects indicators that start
# after login (see README). Make it rescan so the icon appears right away.
gnome_rescan_if_needed() {
    command -v gdbus >/dev/null && command -v gnome-extensions >/dev/null || return 0
    local out
    out=$(gdbus call --session --dest org.kde.StatusNotifierWatcher \
        --object-path /StatusNotifierWatcher \
        --method org.kde.StatusNotifierWatcher.RegisterStatusNotifierItem "/$APP-probe" 2>&1 || true)
    case "$out" in *UnknownMethod*) ;; *) return 0 ;; esac
    local ext
    for ext in ubuntu-appindicators@ubuntu.com appindicatorsupport@rgcjonas.gmail.com; do
        if gnome-extensions info "$ext" 2>/dev/null | grep -q "State: ACTIVE"; then
            say "Reloading the GNOME AppIndicator extension so the icon shows up now"
            sleep 2  # let the widget create its indicator first
            gnome-extensions disable "$ext" && sleep 1 && gnome-extensions enable "$ext"
            return 0
        fi
    done
}

linux_install() {
    linux_paths
    local py
    if ! py=$(linux_python); then
        warn "PyGObject/AppIndicator not found, so the top-bar icon can't be shown."
        warn "Install them with:  $(linux_missing_deps_hint)"
        warn "Continuing; the widget will use a floating window until they are installed."
        py=$(command -v python3 || echo /usr/bin/python3)
    fi

    say "Installing to $DEST"
    stop_running
    mkdir -p "$DEST" "$(dirname "$ICON")"
    install -m 0755 "$SRC_DIR/usage_widget.py" "$DEST/usage_widget.py"
    install -m 0644 "$SRC_DIR/assets/$APP.svg" "$ICON"

    write_desktop_entry "$LAUNCHER" "$py" ""
    write_desktop_entry "$AUTOSTART" "$py" "X-GNOME-Autostart-enabled=true"
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$(dirname "$LAUNCHER")" || true
    say "Added to login startup: $AUTOSTART"

    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        say "Starting it now"
        setsid "$py" "$DEST/usage_widget.py" >/dev/null 2>&1 < /dev/null &
        gnome_rescan_if_needed
    fi
    say "Done. It will start automatically each time you log in."
}

linux_uninstall() {
    linux_paths
    say "Stopping and removing LLM Usage Widget"
    stop_running
    rm -f "$AUTOSTART" "$LAUNCHER" "$ICON"
    rm -rf "$DEST"
    say "Removed. Settings remain in ${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
}

# ---------------------------------------------------------------- macOS

mac_paths() {
    DEST="$HOME/Library/Application Support/$APP/app"
    LABEL="local.$APP"
    PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
    LOG="$HOME/Library/Logs/$APP.log"
}

mac_install() {
    mac_paths
    local py
    py=$(command -v python3) || die "python3 not found. Install it from https://www.python.org/downloads/macos/"
    "$py" -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "Python 3.9 or newer is required ($py is older)"

    say "Installing to $DEST"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    stop_running
    mkdir -p "$DEST"
    install -m 0755 "$SRC_DIR/usage_widget.py" "$DEST/usage_widget.py"

    say "Creating virtualenv and installing pystray + Pillow"
    [ -x "$DEST/venv/bin/python" ] || "$py" -m venv "$DEST/venv"
    "$DEST/venv/bin/python" -m pip install --quiet --upgrade pip
    "$DEST/venv/bin/python" -m pip install --quiet -r "$SRC_DIR/requirements.txt"

    mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$DEST/venv/bin/python</string>
    <string>$DEST/usage_widget.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <!-- Restart after a crash, but not after choosing Quit from the menu. -->
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
EOF
    say "Registering login item: $PLIST"
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    say "Done. It's in the menu bar now and will start each time you log in."
    say "If macOS asks to allow access to \"Claude Code-credentials\", choose Always Allow."
}

mac_uninstall() {
    mac_paths
    say "Stopping and removing LLM Usage Widget"
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    stop_running
    rm -f "$PLIST"
    rm -rf "$(dirname "$DEST")/app"
    say "Removed. Settings remain in ~/Library/Application Support/$APP"
}

# ---------------------------------------------------------------- main

action=install
case "${1:-}" in
    "") ;;
    --uninstall|uninstall) action=uninstall ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
esac

case "$(uname -s)" in
    Linux)  "linux_$action" ;;
    Darwin) "mac_$action" ;;
    *) die "unsupported OS: $(uname -s). On Windows, run install.ps1 (or double-click install.cmd)." ;;
esac
