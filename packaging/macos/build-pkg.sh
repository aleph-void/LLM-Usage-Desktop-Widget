#!/usr/bin/env bash
# Build dist/LLM-Usage-Widget-<version>-macos-<arch>.pkg (run on macOS).
#
# Freezes the app with PyInstaller into "LLM Usage.app" (menu-bar only, no Dock
# icon) and wraps it in an installer that puts it in /Applications and registers
# a LaunchAgent so it starts for every user at login.
#
# Needs: python3 with `pip install pyinstaller -r requirements.txt`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VERSION="${VERSION:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/usage_widget.py")}"
ARCH="$(uname -m)"
NAME="LLM Usage"
BUNDLE_ID="local.llm-usage-widget"
LABEL="$BUNDLE_ID.app"  # differs from install.sh's per-user agent so both can coexist
BUILD="$ROOT/build/macos"
APP_PATH="$BUILD/dist/$NAME.app"

rm -rf "$BUILD"
python3 "$ROOT/packaging/make_icon.py" >/dev/null

python3 -m PyInstaller --noconfirm --clean --windowed \
    --name "$NAME" \
    --icon "$ROOT/assets/icon.png" \
    --osx-bundle-identifier "$BUNDLE_ID" \
    --hidden-import pystray._darwin \
    --distpath "$BUILD/dist" --workpath "$BUILD/work" --specpath "$BUILD" \
    "$ROOT/usage_widget.py"

PLIST="$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VERSION" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $VERSION" "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string $VERSION" "$PLIST"
# Editing Info.plist invalidates PyInstaller's ad-hoc signature; Apple Silicon refuses unsigned code.
codesign --force --deep --sign "${CODESIGN_IDENTITY:--}" "$APP_PATH"

# ---- package payload and scripts
mkdir -p "$BUILD/root/Applications" "$BUILD/scripts"
ditto "$APP_PATH" "$BUILD/root/Applications/$NAME.app"

# UIDs with a GUI login session: one loginwindow per session, including fast user switching.
GUI_UIDS="gui_uids() { ps -axo uid=,comm= | awk '\$2 ~ /loginwindow\$/ && \$1 > 0 {print \$1}' | sort -u; }"

cat > "$BUILD/scripts/preinstall" <<EOF
#!/bin/sh
# Stop the running copies so their files can be replaced. Unload the agent first:
# its KeepAlive would relaunch a killed copy while the payload is being copied.
$GUI_UIDS
for uid in \$(gui_uids); do
    launchctl bootout "gui/\$uid/$LABEL" 2>/dev/null || true
done
pkill -f "/Applications/$NAME.app/Contents/MacOS/" 2>/dev/null || true
exit 0
EOF

cat > "$BUILD/scripts/postinstall" <<EOF
#!/bin/sh
AGENT="/Library/LaunchAgents/$LABEL.plist"
cat > "\$AGENT" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/Applications/$NAME.app/Contents/MacOS/$NAME</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
PLIST
chown root:wheel "\$AGENT"
chmod 0644 "\$AGENT"

# Start it now for everyone who is logged in (preinstall unloaded it for them).
$GUI_UIDS
for uid in \$(gui_uids); do
    launchctl bootout "gui/\$uid/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/\$uid" "\$AGENT" || true
done
exit 0
EOF
chmod 0755 "$BUILD/scripts/preinstall" "$BUILD/scripts/postinstall"

# Install to /Applications even if a copy with the same bundle id exists elsewhere.
pkgbuild --analyze --root "$BUILD/root" "$BUILD/component.plist" >/dev/null
plutil -replace 0.BundleIsRelocatable -bool NO "$BUILD/component.plist"

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/LLM-Usage-Widget-$VERSION-macos-$ARCH.pkg"
SIGN_ARGS=()
[ -n "${INSTALLER_SIGN_IDENTITY:-}" ] && SIGN_ARGS=(--sign "$INSTALLER_SIGN_IDENTITY")
pkgbuild --root "$BUILD/root" \
    --component-plist "$BUILD/component.plist" \
    --scripts "$BUILD/scripts" \
    --identifier "$BUNDLE_ID" \
    --version "$VERSION" \
    --install-location / \
    ${SIGN_ARGS[@]+"${SIGN_ARGS[@]}"} \
    "$OUT"
echo "$OUT"
