#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="${SUDO_USER:-$USER}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PORT="${IP_PLAN_PORT:-5080}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "python3 is required"
  exit 1
fi

if [ ! -d "$APP_DIR/.venv" ]; then
  "$PYTHON_BIN" -m venv "$APP_DIR/.venv" || {
    echo "Failed to create venv. Install python3-venv and rerun."
    exit 1
  }
fi

"$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"

mkdir -p "$APP_DIR/data/projects" "$APP_DIR/data/backups"
chown -R "$APP_USER":"$(id -gn "$APP_USER")" "$APP_DIR/data"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

$SUDO tee /etc/systemd/system/ip-plan-manager.service >/dev/null <<EOF
[Unit]
Description=IP Plan Manager
After=network.target

[Service]
Type=simple
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=IP_PLAN_DATA_DIR=$APP_DIR/data
ExecStart=$APP_DIR/.venv/bin/gunicorn --workers 2 --threads 4 --worker-class gthread --bind 0.0.0.0:$PORT --timeout 60 --access-logfile - --error-logfile - app:app
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

$SUDO tee /etc/systemd/system/ip-plan-manager-backup.service >/dev/null <<EOF
[Unit]
Description=IP Plan Manager daily backup
After=ip-plan-manager.service

[Service]
Type=oneshot
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=IP_PLAN_DATA_DIR=$APP_DIR/data
Environment=IP_PLAN_BACKUP_KEEP_DAYS=30
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/backup.py
EOF

$SUDO tee /etc/systemd/system/ip-plan-manager-backup.timer >/dev/null <<'EOF'
[Unit]
Description=Run IP Plan Manager backup every day at 00:00

[Timer]
OnCalendar=*-*-* 00:00:00
Persistent=true
AccuracySec=1min
Unit=ip-plan-manager-backup.service

[Install]
WantedBy=timers.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now ip-plan-manager.service
$SUDO systemctl enable --now ip-plan-manager-backup.timer

echo
echo "IP Plan Manager installed."
echo "Service:  ip-plan-manager.service"
echo "Backups:  ip-plan-manager-backup.timer (daily at 00:00 server local time)"
echo "URL:      http://SERVER_IP:$PORT"
echo
echo "Check status:"
echo "  systemctl status ip-plan-manager"
echo "  systemctl list-timers ip-plan-manager-backup.timer"
