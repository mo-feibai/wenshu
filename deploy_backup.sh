#!/bin/bash
set -uo pipefail
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST=/opt/wenshu/app
cp "$SRC/backup.sh" "$DST/backup.sh"
sed -i 's/\r$//' "$DST/backup.sh"
chmod +x "$DST/backup.sh"
cp "$SRC/wenshu-backup.service" "$SRC/wenshu-backup.timer" /etc/systemd/system/
sed -i 's/\r$//' /etc/systemd/system/wenshu-backup.service /etc/systemd/system/wenshu-backup.timer
systemctl daemon-reload
echo '--- 立即执行一次备份 ---'
bash "$DST/backup.sh"
echo '--- 启用每日定时器 ---'
systemctl enable --now wenshu-backup.timer
systemctl list-timers wenshu-backup.timer --no-pager | head -4
if [ -n "${WENSHU_BACKUP_WIN:-}" ]; then
  echo '--- 异地备份目录 ---'
  ls -lh "$WENSHU_BACKUP_WIN" | tail -3
fi
