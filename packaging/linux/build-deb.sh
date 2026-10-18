#!/usr/bin/env bash
# Build dist/llm-usage-widget_<version>_all.deb.
#
# The package uses the system Python with PyGObject + AppIndicator (pulled in as
# dependencies) and starts for every user at login via /etc/xdg/autostart.
set -euo pipefail
umask 022

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP=llm-usage-widget
MAINTAINER="${MAINTAINER:-$(git config user.name 2>/dev/null || echo "LLM Usage Widget") <$(git config user.email 2>/dev/null || echo starke.nicholas@gmail.com)>}"
VERSION="${VERSION:-$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/usage_widget.py")}"
STAGE="$(mktemp -d)"
chmod 0755 "$STAGE"
trap 'rm -rf "$STAGE"' EXIT

install -Dm0644 "$ROOT/usage_widget.py"            "$STAGE/usr/lib/$APP/usage_widget.py"
install -Dm0644 "$ROOT/assets/$APP.svg"            "$STAGE/usr/share/icons/hicolor/scalable/apps/$APP.svg"
install -Dm0644 "$ROOT/README.md"                  "$STAGE/usr/share/doc/$APP/README.md"

install -d "$STAGE/usr/bin"
cat > "$STAGE/usr/bin/$APP" <<EOF
#!/bin/sh
exec /usr/bin/python3 /usr/lib/$APP/usage_widget.py "\$@"
EOF
chmod 0755 "$STAGE/usr/bin/$APP"

desktop_entry() {
    cat <<EOF
[Desktop Entry]
Type=Application
Name=LLM Usage Widget
Comment=Claude and Codex usage limits in the top bar
Exec=$APP
Icon=$APP
Terminal=false
Categories=Utility;
EOF
}
install -d "$STAGE/usr/share/applications" "$STAGE/etc/xdg/autostart"
desktop_entry > "$STAGE/usr/share/applications/$APP.desktop"
{ desktop_entry; echo "X-GNOME-Autostart-enabled=true"; } > "$STAGE/etc/xdg/autostart/$APP.desktop"

install -d "$STAGE/DEBIAN"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: $APP
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), python3-gi, gir1.2-ayatanaappindicator3-0.1 | gir1.2-appindicator3-0.1
Recommends: python3-tk, python3-boto3, gnome-shell-extension-appindicator | gnome-shell-extension-ubuntu-appindicators
Maintainer: $MAINTAINER
Description: Claude and Codex usage limits in the desktop top bar
 Shows the session and weekly usage limits of your Claude and Codex (ChatGPT)
 subscriptions in the top bar, using the logins Claude Code and the Codex CLI
 already store on this machine. Starts automatically at login.
EOF
cat > "$STAGE/DEBIAN/conffiles" <<EOF
/etc/xdg/autostart/$APP.desktop
EOF
cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e

# Ubuntu's AppIndicators extension on GNOME 50 ignores indicators that start
# after login (see README). Run in the user's session: reload it once if broken.
RESCAN='
command -v gdbus >/dev/null && command -v gnome-extensions >/dev/null || exit 0
sleep 3
case "$(gdbus call --session --dest org.kde.StatusNotifierWatcher \
        --object-path /StatusNotifierWatcher \
        --method org.kde.StatusNotifierWatcher.RegisterStatusNotifierItem /llm-usage-widget-probe 2>&1)" in
    *UnknownMethod*) ;;
    *) exit 0 ;;
esac
for ext in ubuntu-appindicators@ubuntu.com appindicatorsupport@rgcjonas.gmail.com; do
    if gnome-extensions info "$ext" 2>/dev/null | grep -q "State: ACTIVE"; then
        gnome-extensions disable "$ext" && sleep 1 && gnome-extensions enable "$ext"
        exit 0
    fi
done'

# Start the widget now for every user with a graphical session, instead of
# waiting for their next login. Runs under each user's systemd manager, which
# has the session's DISPLAY/WAYLAND_DISPLAY and D-Bus address.
start_in_sessions() {
    command -v loginctl >/dev/null && command -v systemd-run >/dev/null || return 0
    for uid in $(loginctl list-users --no-legend 2>/dev/null | awk '{print $1}'); do
        [ -n "$(loginctl show-user "$uid" -p Display --value 2>/dev/null)" ] || continue
        user=$(id -nu "$uid" 2>/dev/null) || continue
        systemd-run --user --machine="$user@.host" --quiet --collect \
            --description="LLM Usage Widget" /usr/bin/llm-usage-widget || true
        systemd-run --user --machine="$user@.host" --quiet --collect \
            /bin/sh -c "$RESCAN" || true
    done
}

if [ "$1" = configure ]; then
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
    command -v update-desktop-database >/dev/null && update-desktop-database -q /usr/share/applications || true
    start_in_sessions
fi
EOF
cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
# Stop running copies so the files can be removed cleanly.
pkill -f '/usr/lib/llm-usage-widget/usage_widget.py' 2>/dev/null || true
EOF
chmod 0755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"

mkdir -p "$ROOT/dist"
OUT="$ROOT/dist/${APP}_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT" >/dev/null
echo "$OUT"
