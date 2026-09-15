#!/bin/bash
# 文枢每日备份：文档库 + 服务配置 → WSL 本地 + 可选异地副本（带校验）
# 异地目录通过环境变量 WENSHU_BACKUP_WIN 指定（如 /mnt/e/wenshu-backups）；未设置则跳过。
set -uo pipefail

STAMP=$(date +%Y%m%d-%H%M)
DEST=/opt/wenshu/data/backups
WIN="${WENSHU_BACKUP_WIN:-}"
NAME="wenshu-$STAMP.tar.gz"

mkdir -p "$DEST"
if [ -n "$WIN" ]; then mkdir -p "$WIN"; fi

TAR="$DEST/$NAME"
tar -C /opt/wenshu -czf "$TAR" \
  --exclude='app/node_modules' \
  --exclude='app/dist' \
  --exclude='app/.astro' \
  docs app 2>/dev/null

if [ ! -s "$TAR" ]; then
  echo "ERROR: 备份创建失败 $TAR"
  exit 1
fi

if ! tar -tzf "$TAR" >/dev/null 2>&1; then
  echo "ERROR: 备份文件损坏 $TAR"
  exit 1
fi

echo "本地备份: $TAR ($(du -h "$TAR" | cut -f1))"

if [ -z "$WIN" ]; then
  echo "WARN: 未设置 WENSHU_BACKUP_WIN（跳过异地副本）"
elif cp "$TAR" "$WIN/" 2>/dev/null; then
  H1=$(sha256sum "$TAR" | awk '{print $1}')
  H2=$(sha256sum "$WIN/$NAME" | awk '{print $1}')
  if [ "$H1" = "$H2" ]; then
    echo "异地副本校验通过: $WIN/$NAME"
  else
    echo "WARN: 异地副本哈希不一致（建议重跑）"
  fi
else
  echo "WARN: 无法写入异地目录 $WIN（跳过异地副本）"
fi

ls -1t "$DEST"/wenshu-*.tar.gz 2>/dev/null | tail -n +15 | while read -r f; do rm -f "$f"; done
if [ -n "$WIN" ]; then
  ls -1t "$WIN"/wenshu-*.tar.gz 2>/dev/null | tail -n +15 | while read -r f; do rm -f "$f"; done
fi

echo "保留最近 14 份；完成时间 $(date '+%F %T')"
