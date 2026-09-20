#!/usr/bin/env bash
# Build the epaper-dashboard .deb.
#
# Pure-Python payload, so the package is Architecture: all - the arm64-ness of
# the target is the SoC, not the package. Run from the repo root:
#     packaging/build-deb.sh [version]
set -euo pipefail

VERSION="${1:-0.1.0}"
PKG="epaper-dashboard"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

DEST="$BUILD/$PKG"
install -d "$DEST/DEBIAN"
install -d "$DEST/opt/$PKG"
install -d "$DEST/etc/$PKG"
install -d "$DEST/var/lib/$PKG"
install -d "$DEST/lib/systemd/system"

# --- payload -----------------------------------------------------------------
# Only the runtime pieces. Anything holding credentials or local state is
# deliberately excluded (see the --exclude list) so the package never ships
# someone's tokens.
for item in main.py claude.py codex.py antigravity.py lib icons fnt; do
    [ -e "$ROOT/$item" ] || continue
    cp -r "$ROOT/$item" "$DEST/opt/$PKG/"
done

find "$DEST/opt/$PKG" \
    \( -name '__pycache__' -o -name '*.pyc' -o -name '*.orig' \) -prune -exec rm -rf {} + 2>/dev/null || true

for secret in config.py '*token*.json' 'claude_creds.json' '*.pkl' 'usage.json' 'auth.json' '*.log'; do
    find "$DEST/opt/$PKG" -maxdepth 1 -name "$secret" -delete 2>/dev/null || true
done

install -m 0644 "$ROOT/packaging/config.example.py" "$DEST/etc/$PKG/config.py"
install -m 0644 "$ROOT/packaging/epaper-dashboard.service" "$DEST/lib/systemd/system/$PKG.service"

# --- control -----------------------------------------------------------------
INSTALLED_KB="$(du -ks "$DEST" | cut -f1)"
cat > "$DEST/DEBIAN/control" <<EOF
Package: $PKG
Version: $VERSION
Section: misc
Priority: optional
Architecture: all
Depends: python3 (>= 3.9), python3-pil, python3-numpy, python3-requests, python3-spidev, python3-libgpiod
Recommends: python3-pip
Maintainer: epaper-dashboard contributors <noreply@example.com>
Installed-Size: $INSTALLED_KB
Description: E-ink dashboard for Waveshare 10.85" e-Paper panels
 Renders weather, air quality, device status and service usage to a
 Waveshare 10.85" e-Paper HAT+, including the 4-colour (G) variant.
 Runs as a systemd service and supports Raspberry Pi and Orange Pi
 boards via a libgpiod backend.
EOF

# config.py is a conffile so dpkg leaves local edits alone across upgrades
echo "/etc/$PKG/config.py" > "$DEST/DEBIAN/conffiles"

cat > "$DEST/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    chmod 0750 /var/lib/epaper-dashboard || true
    chmod 0640 /etc/epaper-dashboard/config.py || true
    systemctl daemon-reload || true
    systemctl enable epaper-dashboard.service || true
    echo "epaper-dashboard installed."
    echo "  1. edit /etc/epaper-dashboard/config.py"
    echo "  2. systemctl start epaper-dashboard"
fi
EOF

cat > "$DEST/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ]; then
    systemctl stop epaper-dashboard.service || true
    systemctl disable epaper-dashboard.service || true
fi
EOF

cat > "$DEST/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
systemctl daemon-reload || true
# purge drops local state, but never silently on a plain remove
if [ "$1" = "purge" ]; then
    rm -rf /var/lib/epaper-dashboard
    rm -rf /etc/epaper-dashboard
    # Python writes __pycache__ next to the code at runtime; dpkg did not
    # install those files so it will not remove them, leaving /opt behind.
    rm -rf /opt/epaper-dashboard
fi
EOF

chmod 0755 "$DEST/DEBIAN/postinst" "$DEST/DEBIAN/prerm" "$DEST/DEBIAN/postrm"

OUT="${OUTPUT_DIR:-$ROOT/dist}"
mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$DEST" "$OUT/${PKG}_${VERSION}_all.deb" >/dev/null
echo "built $OUT/${PKG}_${VERSION}_all.deb"
