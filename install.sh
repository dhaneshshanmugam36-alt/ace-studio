#!/usr/bin/env bash
# Installs Ace Studio for the current user.
# Supported: Debian/Ubuntu-based distros (Zorin OS, Ubuntu, Pop!_OS, Linux Mint, Debian).
# Run it while connected to the internet, not to the camera's Wi-Fi.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/ace-studio"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

stop() { echo; echo "Ace Studio can't be installed: $1"; echo; exit 1; }

# --- Checks before touching anything -------------------------------------
[ "$(id -u)" -eq 0 ] && stop "run this as your normal user, not with sudo. It asks for your password when it needs it."

if ! command -v apt-get >/dev/null 2>&1; then
  . /etc/os-release 2>/dev/null || true
  stop "${PRETTY_NAME:-this distro} isn't supported yet.
Version 1 supports Debian/Ubuntu-based systems (Zorin OS, Ubuntu, Pop!_OS, Linux Mint, Debian).
Fedora and Arch support is planned for version 2. See the Roadmap section in the README."
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "==> Python 3 is missing; it will be installed with the other packages."
else
  PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || stop "Python $PYV is too old. Ace Studio needs Python 3.10 or newer (Ubuntu 22.04 / Zorin OS 17 or later)."
  # The camera library pins numpy 1.x, which has no ready-made build for Python 3.13+.
  python3 -c 'import sys; sys.exit(0 if sys.version_info < (3, 13) else 1)' \
    || stop "Python $PYV isn't supported yet (a library Ace Studio depends on needs Python 3.10 to 3.12).
Support for newer Python is planned for version 2."
fi

# --- Install -------------------------------------------------------------
echo "==> Installing system packages (asks for your password once)"
sudo apt-get update -qq
sudo apt-get install -y ffmpeg python3 python3-venv python3-pip python3-gi gir1.2-gtk-3.0 \
  || stop "the system packages didn't install. Check your internet connection and try again."
# WebKit for the app's own window; the package name differs between releases.
sudo apt-get install -y gir1.2-webkit2-4.1 2>/dev/null \
  || sudo apt-get install -y gir1.2-webkit2-4.0 2>/dev/null \
  || echo "   (No WebKitGTK found. Ace Studio will open in your browser instead.)"

echo "==> Copying the app to $DEST"
mkdir -p "$DEST" "$BIN" "$APPS"
cp -r "$SRC/app.py" "$SRC/static" "$SRC/icon.svg" "$DEST/"

echo "==> Setting up Python packages (this takes a few minutes the first time)"
# --system-site-packages lets the window use the system's GTK/WebKit bindings.
python3 -m venv --system-site-packages "$DEST/venv"
"$DEST/venv/bin/pip" install -q --upgrade pip
"$DEST/venv/bin/pip" install -q flask insta360 "pywebview>=5" \
  || stop "the Python packages didn't install. Check your internet connection and run ./install.sh again."

cat > "$BIN/ace-studio" <<EOF
#!/usr/bin/env bash
exec "$DEST/venv/bin/python" "$DEST/app.py" "\$@"
EOF
chmod +x "$BIN/ace-studio"

cat > "$APPS/ace-studio.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Ace Studio
Comment=Live view, recording and file copy for the Insta360 Ace Pro 2 (unofficial)
Exec=$BIN/ace-studio
Icon=$DEST/icon.svg
Terminal=false
Categories=AudioVideo;Video;Photography;
StartupWMClass=Ace Studio
EOF
update-desktop-database "$APPS" 2>/dev/null || true

echo
echo "Done. Open \"Ace Studio\" from the app menu, or run:  ace-studio"
echo "Try it without a camera:  ace-studio --demo"
case ":$PATH:" in
  *":$BIN:"*) ;;
  *) echo "(If the ace-studio command isn't found, log out and back in once.)" ;;
esac
