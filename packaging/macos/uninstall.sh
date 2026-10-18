#!/bin/sh
# Remove the LLM Usage Widget that the macOS .pkg installed: stop it for every
# logged-in user, then delete the app, its LaunchAgent and the package receipt.
#
#   sudo "/Applications/LLM Usage.app/Contents/Resources/uninstall.sh"
#
# The same script is the postinstall of the payload-free uninstaller pkg that
# build-pkg.sh produces. Per-user settings in ~/Library/Application Support/
# llm-usage-widget are left in place, as are copies installed by install.sh.
set -u

NAME="LLM Usage"
BUNDLE_ID="local.llm-usage-widget"
LABEL="$BUNDLE_ID.app"
APP="/Applications/$NAME.app"
AGENT="/Library/LaunchAgents/$LABEL.plist"

if [ "$(id -u)" -ne 0 ]; then
    exec sudo /bin/sh "$0" "$@"
fi

# UIDs with a GUI login session: one loginwindow per session, including fast user switching.
gui_uids() { ps -axo uid=,comm= | awk '$2 ~ /loginwindow$/ && $1 > 0 {print $1}' | sort -u; }

# Unload the agent first: its KeepAlive would relaunch a killed copy.
for uid in $(gui_uids); do
    launchctl bootout "gui/$uid/$LABEL" 2>/dev/null || true
done
pkill -f "$APP/Contents/MacOS/" 2>/dev/null || true

rm -f "$AGENT"
rm -rf "$APP"
pkgutil --forget "$BUNDLE_ID" >/dev/null 2>&1 || true

echo "LLM Usage Widget removed. Settings remain in ~/Library/Application Support/llm-usage-widget"
exit 0
