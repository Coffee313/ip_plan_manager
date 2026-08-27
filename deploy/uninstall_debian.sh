#!/usr/bin/env bash
set -euo pipefail
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi
$SUDO systemctl disable --now ip-plan-manager.service 2>/dev/null || true
$SUDO systemctl disable --now ip-plan-manager-backup.timer 2>/dev/null || true
$SUDO rm -f /etc/systemd/system/ip-plan-manager.service
$SUDO rm -f /etc/systemd/system/ip-plan-manager-backup.service
$SUDO rm -f /etc/systemd/system/ip-plan-manager-backup.timer
$SUDO systemctl daemon-reload
echo "Services removed. Application files and data were not deleted."
