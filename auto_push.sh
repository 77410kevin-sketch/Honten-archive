#!/bin/bash
# 自動備份腳本：有變更就 commit + push 到 GitHub

REPO="/Users/kevin/Projects/HonTen"
LOG="$REPO/.auto_push.log"

cd "$REPO" || exit 1

# 載入 SSH key
eval "$(ssh-agent -s)" > /dev/null 2>&1
ssh-add /Users/kevin/.ssh/id_ed25519 > /dev/null 2>&1

# 檢查是否有未提交的變更
if git status --porcelain | grep -q .; then
  TIMESTAMP=$(date '+%Y-%m-%d %H:%M')
  git add .
  git commit -m "自動備份 $TIMESTAMP"
  git push origin main
  echo "[$TIMESTAMP] ✅ 已自動 push" >> "$LOG"
else
  echo "[$(date '+%Y-%m-%d %H:%M')] — 無變更，略過" >> "$LOG"
fi
