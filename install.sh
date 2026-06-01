#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════
#  Hikvision Monitor — One-click installer
#  Use: bash install.sh
# ═══════════════════════════════════════════════════════════════
set -e

REPO="https://github.com/manutdsnake/Hik-monitor"
INSTALL_DIR="$HOME/hikvision-monitor"
SDK_TARGET="$HOME/Desktop/sdk"
VENV_DIR="$INSTALL_DIR/venv"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓  $*${NC}"; }
info() { echo -e "${CYAN}  →  $*${NC}"; }

clear
echo -e "\n${BOLD}  ╔══════════════════════════════════════╗"
echo -e "  ║   Hikvision Monitor — Install    ║"
echo -e "  ╚══════════════════════════════════════╝${NC}\n"

# ── 1. System packages ────────────────────────────────────────
info "Installing system packages... (takes some time)"
sudo apt-get update -qq 2>/dev/null
sudo apt-get install -y -qq \
    git python3 python3-venv python3-dev \
    libgl1 libglib2.0-0 ffmpeg \
    libxcb-xinerama0 libxcb-cursor0 2>/dev/null
ok "System packages OK"

# ── 2. Get repo ──────────────────────────────────────────────
info "Downloading app from GitHub..."
if [[ -d "$INSTALL_DIR/.git" ]]; then
    git -C "$INSTALL_DIR" pull -q
    ok "App updated"
else
    rm -rf "$INSTALL_DIR"
    git clone -q "$REPO" "$INSTALL_DIR"
    ok "App downloaded"
fi

# ── 3. Virtual environment ─────────────────────────────────────
info "Setting Python virtual environment..."
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
fi
ok "Virtual environment OK"

# ── 4. Python packages venv ────────────────────────────────────
info "Installing Python pakete to venv..."
"$VENV_DIR/bin/pip" install -q --upgrade pip
"$VENV_DIR/bin/pip" install -q PyQt5 requests numpy opencv-python psutil
ok "Python paketi OK"

# ── 5. Setup SDK ─────────────────────────────────────────────
info "Setting up Hikvision SDK..."
mkdir -p "$HOME/Desktop"
if [[ -d "$SDK_TARGET" ]]; then
    ok "SDK already exists @ $SDK_TARGET"
else
    cp -r "$INSTALL_DIR/sdk" "$SDK_TARGET"
    ok "SDK copied to $SDK_TARGET"
fi

# ── 6. Launcher (auto venv) ──────────────────────
info "Creating launcher..."
cat > "$INSTALL_DIR/start.sh" << LAUNCHER
#!/usr/bin/env bash
cd "$INSTALL_DIR"
SDK="$HOME/Desktop/sdk/lib"
if [[ -d "\$SDK" ]]; then
    export LD_LIBRARY_PATH="\$SDK:\$SDK/HCNetSDKCom:\$LD_LIBRARY_PATH"
fi
export QT_LOGGING_RULES="qt.qpa.wayland=false"
exec "$VENV_DIR/bin/python3" hik_monitor2.py "\$@"
LAUNCHER
chmod +x "$INSTALL_DIR/start.sh"
ok "Launcher created"

# ── 7. Desktop icon ───────────────────────────────────────────
DESKTOP="$HOME/Desktop/Hikvision Monitor.desktop"
cat > "$DESKTOP" << DFILE
[Desktop Entry]
Version=1.0
Type=Application
Name=Hikvision Monitor
Comment=Hikvision NVR Monitor
Exec=$INSTALL_DIR/start.sh
Icon=camera-video
Terminal=false
Categories=Video;Network;
DFILE
chmod +x "$DESKTOP"
gio set "$DESKTOP" metadata::trusted true 2>/dev/null || true
ok "Icon placed to Desktop"

# ── Done ─────────────────────────────────────────────────────
echo -e "\n${BOLD}  ╔══════════════════════════════════════╗"
echo -e "  ║          Install complete!        ║"
echo -e "  ╚══════════════════════════════════════╝${NC}\n"
echo -e "  ${GREEN}Start app: 'Hikvision Monitor' @ Desktop${NC}\n"
