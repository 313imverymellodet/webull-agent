#!/usr/bin/env bash
# One-time setup for a fresh Hostinger VPS (Ubuntu 22.04/24.04). Run as root:
#   bash server-setup.sh
# Idempotent: safe to re-run.
set -euo pipefail
APP=/opt/webull-agent
USER_NAME=trader

echo "→ Packages and automatic security updates"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq python3-venv python3-pip rsync ufw fail2ban unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "→ Eastern time (the runners read the local clock for market hours)"
timedatectl set-timezone America/New_York

echo "→ Unprivileged '$USER_NAME' user; same SSH keys as root"
id -u $USER_NAME >/dev/null 2>&1 || adduser --disabled-password --gecos "" $USER_NAME
install -d -m 700 -o $USER_NAME -g $USER_NAME /home/$USER_NAME/.ssh
if [ -s /root/.ssh/authorized_keys ]; then
  install -m 600 -o $USER_NAME -g $USER_NAME /root/.ssh/authorized_keys /home/$USER_NAME/.ssh/authorized_keys
fi

echo "→ SSH: keys only (skipped if no key is installed, so you can't get locked out)"
if [ -s /root/.ssh/authorized_keys ]; then
  cat > /etc/ssh/sshd_config.d/10-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  systemctl reload ssh 2>/dev/null || systemctl reload sshd
else
  echo "  !! no SSH key in /root/.ssh/authorized_keys — password login left ON. Add a key, then re-run."
fi

echo "→ Firewall: nothing inbound except SSH (the bots only make outbound calls)"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null

echo "→ App directory and Python environment"
install -d -o $USER_NAME -g $USER_NAME $APP $APP/logs $APP/state
chown -R $USER_NAME:$USER_NAME $APP
[ -f $APP/.env ] && chmod 600 $APP/.env
if [ -f $APP/requirements.txt ]; then
  sudo -u $USER_NAME python3 -m venv $APP/.venv
  sudo -u $USER_NAME $APP/.venv/bin/pip install -q --upgrade pip
  sudo -u $USER_NAME $APP/.venv/bin/pip install -q -r $APP/requirements.txt
else
  echo "  requirements.txt not uploaded yet — run deploy/push.sh from your Mac, then re-run this."
fi

echo "→ systemd services + weekday timers"
unit() {  # name, command, restart policy
cat > /etc/systemd/system/webull-$1.service <<EOF
[Unit]
Description=Webull agent: $1
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP
Environment=TZ=America/New_York
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP/.venv/bin/python -u $2
Restart=$3
RestartSec=30
StandardOutput=append:$APP/logs/$1.log
StandardError=append:$APP/logs/$1.log

[Install]
WantedBy=multi-user.target
EOF
}
# Runners exit on their own at 16:00, so only restart them after a crash.
unit ema       "run_ema.py --mode live"    on-failure
unit miyagi    "run_miyagi.py --mode live" on-failure
unit publisher "publish_snapshot.py"       always

for svc in ema miyagi; do
cat > /etc/systemd/system/webull-$svc.timer <<EOF
[Unit]
Description=Start webull-$svc before the open on weekdays

[Timer]
OnCalendar=Mon..Fri *-*-* 09:00:00 America/New_York
Persistent=true

[Install]
WantedBy=timers.target
EOF
done

systemctl daemon-reload
systemctl enable --now webull-ema.timer webull-miyagi.timer >/dev/null
if grep -qE '^DASHBOARD_URL=.+' $APP/.env 2>/dev/null; then
  systemctl enable --now webull-publisher.service >/dev/null
else
  echo "  publisher not started: set DASHBOARD_URL in .env first"
fi

echo
echo "Done. Check with:  systemctl list-timers 'webull-*'   and   tail -f $APP/logs/ema.log"
