#!/bin/bash
# Deploy sauna_control backend to VPS
# Usage: ./ops/deploy.sh [fresh]
#   No args: update deploy (scp + restart)
#   fresh:   full setup (prereqs, venv, systemd, Caddy)
#
# First-time: add your SSH key for passwordless auth:
#   ssh-copy-id root@74.208.194.144
#
# Load credentials
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
if [[ -f "$SCRIPT_DIR/local-credentials" ]]; then
  source "$SCRIPT_DIR/local-credentials"
else
  echo "Missing ops/local-credentials. Create from ops/local-credentials.example"
  exit 1
fi

VPS="${VPS_USER}@${VPS_HOST}"
APP_DIR="/opt/sauna/backend_ui"

set -e

if [[ "$1" == "fresh" ]]; then
  echo "=== Fresh deploy to $VPS ==="
  echo "Copying backend_ui..."
  ssh -o StrictHostKeyChecking=accept-new "$VPS" "mkdir -p /opt/sauna"
  scp -r -o StrictHostKeyChecking=accept-new "$PROJECT_DIR/backend_ui" "$VPS:/opt/sauna/"

  echo "Running setup on VPS..."
  ssh "$VPS" 'bash -s' << 'REMOTE'
set -e
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv 2>/dev/null || true
cd /opt/sauna/backend_ui
python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "WARNING: Edit /opt/sauna/backend_ui/.env with real SAUNA_DEVICE_TOKEN and SAUNA_APP_TOKEN"
fi
# systemd service
cat > /etc/systemd/system/sauna.service << 'SVCEOF'
[Unit]
Description=Sauna Control API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/sauna/backend_ui
EnvironmentFile=/opt/sauna/backend_ui/.env
ExecStart=/opt/sauna/backend_ui/venv/bin/uvicorn server:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload
systemctl enable sauna
systemctl restart sauna
ufw allow 8000/tcp 2>/dev/null || true
ufw allow 80/tcp 2>/dev/null || true
ufw allow 443/tcp 2>/dev/null || true
echo "Service started. Check: systemctl status sauna"
REMOTE

  echo ""
  echo "=== Caddy (HTTPS) - run manually on VPS if needed ==="
  echo "  apt install -y caddy"
  echo "  Configure /etc/caddy/Caddyfile with sauna.wilsondesignllc.com"
  echo "  systemctl reload caddy"
  echo ""
  echo "=== Cloud firewall: allow inbound TCP 8000, 80, 443 ==="
  echo ""
  echo "Done. UI: http://${VPS_HOST}:8000/ or https://sauna.wilsondesignllc.com/ (after Caddy)"
else
  echo "=== Update deploy to $VPS ==="
  scp -r -o StrictHostKeyChecking=accept-new "$PROJECT_DIR/backend_ui" "$VPS:/opt/sauna/"
  ssh "$VPS" "systemctl restart sauna && systemctl status sauna"
  echo "Done. UI: https://sauna.wilsondesignllc.com/"
fi
