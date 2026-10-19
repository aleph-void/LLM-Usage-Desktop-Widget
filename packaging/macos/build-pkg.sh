#!/usr/bin/env bash
# Build dist/LLM-Usage-Widget-<version>-macos-<arch>.dmg (run on macOS).
#
# Freezes the app with PyInstaller into "LLM Usage.app" (menu-bar only, no Dock
# icon) and wraps it in an installer pkg that puts it in /Applications and registers
# a LaunchAgent so it starts for every user at login.
#
# The disk image holds that pkg and an uninstaller pkg: a payload-free package that
# runs packaging/macos/uninstall.sh (also bundled in the app's Resources).
#
# Needs: python3 with `pip install pyinstaller -r requirements.txt`.
#
# Signing (optional; unset = ad-hoc signed app, unsigned pkg, for local testing):
#   CODESIGN_IDENTITY        "Developer ID Application: ..." (hardened runtime + timestamp)
#   INSTALLER_SIGN_IDENTITY  "Developer ID Installer: ..."
#   SIGNING_KEYCHAIN         keychain holding those identities
# The disk image is signed with CODESIGN_IDENTITY. Notarize and staple it afterwards
# (see .github/workflows/build-installers.yml); that covers the pkgs inside it too.
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
install -m 0755 "$ROOT/packaging/macos/uninstall.sh" "$APP_PATH/Contents/Resources/uninstall.sh"
# Editing Info.plist invalidates PyInstaller's ad-hoc signature; Apple Silicon refuses unsigned code.
if [ -z "${CODESIGN_IDENTITY:-}" ] || [ "$CODESIGN_IDENTITY" = "-" ]; then
    codesign --force --deep --sign - "$APP_PATH"
else
    # Notarization needs every Mach-O signed with the hardened runtime and a timestamp.
    # Sign inside-out: nested libraries first, the bundle (and its main executable) last.
    SIGN=(codesign --force --options runtime --timestamp --sign "$CODESIGN_IDENTITY")
    [ -n "${SIGNING_KEYCHAIN:-}" ] && SIGN+=(--keychain "$SIGNING_KEYCHAIN")
    find "$APP_PATH/Contents" -type f ! -path "$APP_PATH/Contents/MacOS/$NAME" -print0 |
        while IFS= read -r -d '' f; do
            if file -b "$f" | grep -q 'Mach-O'; then "${SIGN[@]}" "$f"; fi
        done
    "${SIGN[@]}" --entitlements "$ROOT/packaging/macos/entitlements.plist" "$APP_PATH"
    codesign --verify --deep --strict --verbose=2 "$APP_PATH"
fi

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

# ---- the two pkgs, under the names they have in the disk image
DMG_ROOT="$BUILD/dmg"
mkdir -p "$DMG_ROOT"
SIGN_ARGS=()
if [ -n "${INSTALLER_SIGN_IDENTITY:-}" ]; then
    SIGN_ARGS=(--sign "$INSTALLER_SIGN_IDENTITY")
    [ -n "${SIGNING_KEYCHAIN:-}" ] && SIGN_ARGS+=(--keychain "$SIGNING_KEYCHAIN")
fi
INSTALL_PKG="$DMG_ROOT/Install LLM Usage Widget.pkg"
pkgbuild --root "$BUILD/root" \
    --component-plist "$BUILD/component.plist" \
    --scripts "$BUILD/scripts" \
    --identifier "$BUNDLE_ID" \
    --version "$VERSION" \
    --install-location / \
    ${SIGN_ARGS[@]+"${SIGN_ARGS[@]}"} \
    "$INSTALL_PKG"

# Uninstaller: no payload (so no receipt), just the uninstall script as postinstall.
mkdir -p "$BUILD/uninstall-scripts"
install -m 0755 "$ROOT/packaging/macos/uninstall.sh" "$BUILD/uninstall-scripts/postinstall"
UNINSTALL_PKG="$DMG_ROOT/Uninstall LLM Usage Widget.pkg"
pkgbuild --nopayload \
    --scripts "$BUILD/uninstall-scripts" \
    --identifier "$BUNDLE_ID.uninstall" \
    --version "$VERSION" \
    ${SIGN_ARGS[@]+"${SIGN_ARGS[@]}"} \
    "$UNINSTALL_PKG"
if [ -n "${INSTALLER_SIGN_IDENTITY:-}" ]; then
    pkgutil --check-signature "$INSTALL_PKG" >&2
    pkgutil --check-signature "$UNINSTALL_PKG" >&2
fi

# ---- disk image
mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/LLM-Usage-Widget-$VERSION-macos-$ARCH.dmg"
hdiutil create -volname "LLM Usage Widget $VERSION" -srcfolder "$DMG_ROOT" \
    -fs HFS+ -format UDZO -ov "$OUT" >/dev/null
if [ -n "${CODESIGN_IDENTITY:-}" ] && [ "$CODESIGN_IDENTITY" != "-" ]; then
    DMG_SIGN=(codesign --force --timestamp --sign "$CODESIGN_IDENTITY")
    [ -n "${SIGNING_KEYCHAIN:-}" ] && DMG_SIGN+=(--keychain "$SIGNING_KEYCHAIN")
    "${DMG_SIGN[@]}" "$OUT"
    codesign --verify --strict --verbose=2 "$OUT"
fi
echo "$OUT"
