#!/bin/bash
# NVRR Deploy Script — Run on Debian 12 server as root
set -e

INSTALL_DIR="/opt/nvrr"
MEDIAMTX_VERSION="1.9.3"

echo "=== NVRR Deployment ==="

# 1. System packages
echo "[1/7] Installing system packages..."
apt update -qq
apt install -y nginx ffmpeg curl build-essential python3-dev

# 2. Install uv
if ! command -v uv &>/dev/null; then
    echo "[2/7] Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
else
    echo "[2/7] uv already installed"
fi

# 3. Create user + directories
if ! id -u nvrr &>/dev/null; then
    echo "[3/7] Creating nvrr user..."
    useradd -r -s /bin/false -d "$INSTALL_DIR" nvrr
else
    echo "[3/7] User nvrr already exists"
fi
mkdir -p "$INSTALL_DIR"/{backend,frontend,config,mediamtx,data,sdk}

# 4. Install MediaMTX
if [ ! -f "$INSTALL_DIR/mediamtx/mediamtx" ]; then
    echo "[4/7] Downloading MediaMTX ${MEDIAMTX_VERSION}..."
    ARCH=$(dpkg --print-architecture)
    case "$ARCH" in
        amd64) MTX_ARCH="linux_amd64" ;;
        arm64) MTX_ARCH="linux_arm64v8" ;;
        armhf) MTX_ARCH="linux_armv7" ;;
        *) echo "Unsupported arch: $ARCH"; exit 1 ;;
    esac
    curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_${MTX_ARCH}.tar.gz" \
        | tar xz -C "$INSTALL_DIR/mediamtx/"
else
    echo "[4/7] MediaMTX already installed"
fi

# 5. Copy application files
echo "[5/7] Copying application files..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp -r "$SCRIPT_DIR/backend/"* "$INSTALL_DIR/backend/"
cp -r "$SCRIPT_DIR/frontend/"* "$INSTALL_DIR/frontend/"
cp "$SCRIPT_DIR/pyproject.toml" "$INSTALL_DIR/pyproject.toml"
cp "$SCRIPT_DIR/config/mediamtx.yml" "$INSTALL_DIR/mediamtx/mediamtx.yml"
# Copy config.json if it exists
if [ -f "$SCRIPT_DIR/config.json" ]; then
    cp "$SCRIPT_DIR/config.json" "$INSTALL_DIR/config.json"
fi
# Copy Linux SDK libs
if [ -d "$SCRIPT_DIR/sdk-linux" ]; then
    cp -r "$SCRIPT_DIR/sdk-linux/"* "$INSTALL_DIR/sdk/"
    chmod +x "$INSTALL_DIR/sdk/"*.so "$INSTALL_DIR/sdk/HCNetSDKCom/"*.so 2>/dev/null || true
    echo "    Linux HCNetSDK copied to $INSTALL_DIR/sdk/"
fi

# 6. Install Python deps via uv
echo "[6/7] Installing Python dependencies via uv..."
cd "$INSTALL_DIR"
uv sync

# 7. Install services
echo "[7/7] Installing services..."
cp "$SCRIPT_DIR/config/nvrr.service" /etc/systemd/system/nvrr.service
cp "$SCRIPT_DIR/config/mediamtx.service" /etc/systemd/system/mediamtx.service
cp "$SCRIPT_DIR/config/nginx.conf" /etc/nginx/sites-available/nvrr
ln -sf /etc/nginx/sites-available/nvrr /etc/nginx/sites-enabled/nvrr
rm -f /etc/nginx/sites-enabled/default

# Fix ownership
chown -R nvrr:nvrr "$INSTALL_DIR"

# Reload and start
systemctl daemon-reload
systemctl enable --now mediamtx nvrr
systemctl restart nginx

echo ""
echo "=== NVRR deployed! ==="
echo "  Viewer:  http://$(hostname -I | awk '{print $1}')"
echo "  Admin:   http://$(hostname -I | awk '{print $1}')/admin.html"
echo ""
echo "  Default admin password: changeme"
echo "  Change it in: /etc/systemd/system/nvrr.service"
echo ""
