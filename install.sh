#!/usr/bin/env bash
# Installs Ace Studio for the current user.
# Works on Debian/Ubuntu-based distros (Zorin OS, Ubuntu, Pop!_OS, Linux Mint, Debian),
# Fedora, and Arch-based distros (Arch, EndeavourOS, Manjaro).
# Run it while connected to the internet, not to the camera's Wi-Fi.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.local/share/ace-studio"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"

stop() { echo; echo "Ace Studio can't be installed: $1"; echo; exit 1; }
step() { echo "==> $1"; }

# --- Checks before touching anything -------------------------------------
[ "$(id -u)" -eq 0 ] && stop "run this as your normal user, not with sudo. It asks for your password when it needs it."

. /etc/os-release 2>/dev/null || true
if command -v apt-get >/dev/null 2>&1; then PM=apt
elif command -v dnf >/dev/null 2>&1; then PM=dnf
elif command -v pacman >/dev/null 2>&1; then PM=pacman
else stop "${PRETTY_NAME:-this distro} isn't supported yet. Ace Studio supports Debian/Ubuntu-based distros, Fedora and Arch-based distros."
fi

# --- System packages -----------------------------------------------------
step "Installing system packages (asks for your password once)"
case "$PM" in
  apt)
    sudo apt-get update -qq
    sudo apt-get install -y ffmpeg python3 python3-venv python3-pip python3-gi gir1.2-gtk-3.0 zenity \
      || stop "the system packages didn't install. Check your internet connection and try again."
    # WebKit for the app's own window; the package name differs between releases.
    sudo apt-get install -y gir1.2-webkit2-4.1 2>/dev/null \
      || sudo apt-get install -y gir1.2-webkit2-4.0 2>/dev/null \
      || echo "   (No WebKitGTK found. Ace Studio will open in your browser instead.)"
    ;;
  dnf)
    sudo dnf install -y python3 python3-pip python3-gobject gtk3 zenity \
      || stop "the system packages didn't install. Check your internet connection and try again."
    sudo dnf install -y webkit2gtk4.1 2>/dev/null || sudo dnf install -y webkit2gtk4.0 2>/dev/null \
      || echo "   (No WebKitGTK found. Ace Studio will open in your browser instead.)"
    # Fedora's own ffmpeg ("ffmpeg-free") can't decode the camera's H.265 video.
    # The full ffmpeg comes from RPM Fusion.
    if ! rpm -q ffmpeg >/dev/null 2>&1; then
      if ! dnf repolist 2>/dev/null | grep -qi rpmfusion-free; then
        step "Adding RPM Fusion (for the full ffmpeg that can play H.265)"
        sudo dnf install -y "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm" \
          || stop "RPM Fusion couldn't be added. See https://rpmfusion.org/Configuration and run ./install.sh again."
      fi
      if rpm -q ffmpeg-free >/dev/null 2>&1; then
        sudo dnf swap -y ffmpeg-free ffmpeg --allowerasing || stop "ffmpeg couldn't be installed from RPM Fusion."
      else
        sudo dnf install -y ffmpeg || stop "ffmpeg couldn't be installed from RPM Fusion."
      fi
    fi
    ;;
  pacman)
    sudo pacman -S --needed --noconfirm ffmpeg python python-pip python-gobject gtk3 zenity \
      || stop "the system packages didn't install. Check your internet connection and try again."
    sudo pacman -S --needed --noconfirm webkit2gtk-4.1 2>/dev/null \
      || echo "   (No WebKitGTK found. Ace Studio will open in your browser instead.)"
    ;;
esac

PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || stop "Python $PYV is too old. Ace Studio needs Python 3.10 or newer."
python3 -m venv --help >/dev/null 2>&1 || stop "Python's venv module is missing (install python3-venv)."

# --- App files -----------------------------------------------------------
step "Copying the app to $DEST"
mkdir -p "$DEST" "$BIN" "$APPS"
rm -rf "$DEST/static"
cp "$SRC"/*.py "$SRC/looks.json" "$SRC/icon.svg" "$DEST/"
cp -r "$SRC/static" "$DEST/"

# --- Python packages -----------------------------------------------------
step "Setting up Python packages (this takes a few minutes the first time)"
# --system-site-packages lets the window use the system's GTK/WebKit bindings.
if [ ! -x "$DEST/venv/bin/python" ] || ! "$DEST/venv/bin/python" -c 'import sys' 2>/dev/null \
   || [ "$("$DEST/venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" != "$PYV" ]; then
  rm -rf "$DEST/venv"
  python3 -m venv --system-site-packages "$DEST/venv"
fi
PIP="$DEST/venv/bin/pip"
"$PIP" install -q --upgrade pip
# The camera library pins old versions (numpy 1.x) that don't build on new Python,
# so it's installed on its own and its real needs are installed next to it.
"$PIP" install -q --no-deps insta360 lib-one-proto \
  || stop "the camera library didn't install. Check your internet connection and run ./install.sh again."
"$PIP" install -q flask requests beautifulsoup4 "protobuf<6" pydantic numpy opencv-python-headless pillow "pywebview>=5" \
  || stop "the Python packages didn't install. Check your internet connection and run ./install.sh again."
echo "   (Any pip warning about insta360 wanting older numpy/opencv is expected and harmless.)"
"$DEST/venv/bin/python" -c 'import insta360, flask, PIL' 2>/dev/null \
  || stop "the Python packages installed but don't load. Please open an issue on GitHub with the output above."

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
