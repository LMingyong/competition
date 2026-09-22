#!/usr/bin/env bash
# 用本地临时仓库检查 auto_git_pull.sh：已最新、拉到新提交、远程不可达、重复启动。
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
SCRIPT="$ROOT/scripts/auto_git_pull.sh"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

git init -b main "$TMP/origin" >/dev/null
git -C "$TMP/origin" config user.email "test@example.com"
git -C "$TMP/origin" config user.name "test"
echo base > "$TMP/origin/f.txt"
git -C "$TMP/origin" add f.txt
git -C "$TMP/origin" commit -m "base" >/dev/null

git clone "$TMP/origin" "$TMP/work" >/dev/null
git -C "$TMP/work" config user.email "test@example.com"
git -C "$TMP/work" config user.name "test"

LOG="$TMP/pull.log"

"$SCRIPT" --once --repo "$TMP/work" --log "$LOG"
grep -q "已是最新" "$LOG"

echo more >> "$TMP/origin/f.txt"
git -C "$TMP/origin" add f.txt
git -C "$TMP/origin" commit -m "second change" >/dev/null

"$SCRIPT" --once --repo "$TMP/work" --log "$LOG"
grep -q "有更新" "$LOG"
grep -q "提交 .* second change" "$LOG"

git -C "$TMP/work" remote set-url origin "$TMP/does-not-exist"
set +e
"$SCRIPT" --once --repo "$TMP/work" --log "$LOG"
code=$?
set -e
[[ "$code" -ne 0 ]]
grep -q "拉取失败" "$LOG"

git -C "$TMP/work" remote set-url origin "$TMP/origin"
"$SCRIPT" --repo "$TMP/work" --log "$LOG" --interval 30 &
loop_pid=$!
ready=0
for _ in 1 2 3 4 5 6 7 8 9 10; do
  if grep -q "开始轮询" "$LOG"; then
    ready=1
    break
  fi
  sleep 0.2
done
[[ "$ready" -eq 1 ]]
set +e
"$SCRIPT" --once --repo "$TMP/work" --log "$LOG"
busy=$?
set -e
[[ "$busy" -eq 1 ]]
kill "$loop_pid" 2>/dev/null || true
pkill -P "$loop_pid" 2>/dev/null || true
wait "$loop_pid" 2>/dev/null || true

echo "ok"
