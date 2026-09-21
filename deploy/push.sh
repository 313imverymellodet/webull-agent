#!/usr/bin/env bash
# Deploy code + .env from this Mac to the VPS and restart what's running.
#   deploy/push.sh root@YOUR_VPS_IP
# Connects as root (key-only), then hands every file to the unprivileged 'trader'
# user the services run as.
set -euo pipefail
HOST=${1:?usage: deploy/push.sh user@host}
KEY=${SSH_KEY:-$HOME/.ssh/webull_vps}
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new"
cd "$(dirname "$0")/.."

echo "→ Syncing code to $HOST:/opt/webull-agent"
$SSH "$HOST" 'install -d /opt/webull-agent'
rsync -az --delete -e "$SSH" \
  --exclude .venv --exclude .git --exclude logs --exclude state --exclude .env \
  --exclude '*.log' --exclude '*.log.*' --exclude __pycache__ --exclude conf \
  --exclude dashboard_data.json --exclude dashboard.html \
  --exclude web/node_modules --exclude web/.vercel \
  ./ "$HOST:/opt/webull-agent/"

echo "→ Uploading .env (owner-only permissions; never committed to git)"
# scp + explicit chmod: macOS rsync has no --chmod
scp -q -i "$KEY" -o StrictHostKeyChecking=accept-new .env "$HOST:/opt/webull-agent/.env"
$SSH "$HOST" 'chmod 600 /opt/webull-agent/.env'

echo "→ Installing Python packages and restarting running services"
$SSH "$HOST" 'cd /opt/webull-agent
  id -u trader >/dev/null 2>&1 && chown -R trader:trader /opt/webull-agent
  [ -x .venv/bin/pip ] && sudo -u trader .venv/bin/pip install -q -r requirements.txt
  for s in webull-ema webull-miyagi webull-publisher; do
    systemctl is-active --quiet $s && systemctl restart $s
  done; echo ok'
