#!/usr/bin/env bash
# 每 5 分钟对仓库执行一次 git pull --ff-only。
# 拉到新提交时把提交写进日志；失败只记录，进程不退出，下一轮继续。
#
# 用法：
#   ./scripts/auto_git_pull.sh
#   ./scripts/auto_git_pull.sh --once
#   INTERVAL=60 ./scripts/auto_git_pull.sh
#
# Windows 用同目录的 auto_git_pull.bat，参数相同。
#
# 选项：
#   --once              只拉取一次后退出
#   --interval <秒>     轮询间隔，默认 300，也可用环境变量 INTERVAL
#   --repo <目录>       仓库目录，默认为本脚本所在仓库根
#   --log <文件>        日志文件，默认 <仓库>/logs/auto_pull.log
#   --remote <名称>     远程名，默认 origin（实际仍走当前分支的上游）
#   -h, --help          显示帮助

set -u

INTERVAL="${INTERVAL:-300}"
ONCE=0
REPO_DIR="${REPO_DIR:-}"
LOG_FILE="${LOG_FILE:-}"
REMOTE="${REMOTE:-origin}"

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --once)
      ONCE=1
      shift
      ;;
    --interval)
      INTERVAL="${2:-}"
      shift 2
      ;;
    --repo)
      REPO_DIR="${2:-}"
      shift 2
      ;;
    --log)
      LOG_FILE="${2:-}"
      shift 2
      ;;
    --remote)
      REMOTE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "未知参数: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ! "$INTERVAL" =~ ^[1-9][0-9]*$ ]]; then
  echo "间隔必须是正整数秒: $INTERVAL" >&2
  exit 2
fi

SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
if [[ -z "$REPO_DIR" ]]; then
  REPO_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
fi
if [[ -z "$LOG_FILE" ]]; then
  LOG_FILE="$REPO_DIR/logs/auto_pull.log"
fi

mkdir -p "$(dirname "$LOG_FILE")"

log() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S') $*"
  printf '%s\n' "$line" | tee -a "$LOG_FILE"
}

pull_once() {
  local before after err commits
  if ! before=$(git -C "$REPO_DIR" rev-parse HEAD 2>&1); then
    log "拉取失败: 不是 git 仓库或没有 HEAD: $before"
    return 1
  fi

  if ! git -C "$REPO_DIR" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
    log "拉取失败: 当前分支没有上游。请先执行 git branch -u ${REMOTE}/<分支>"
    return 1
  fi

  local upstream_remote
  upstream_remote=$(git -C "$REPO_DIR" rev-parse --abbrev-ref --symbolic-full-name '@{u}' | cut -d/ -f1)
  if [[ "$upstream_remote" != "$REMOTE" ]]; then
    log "拉取失败: 当前上游在 ${upstream_remote}，与指定远程 ${REMOTE} 不一致"
    return 1
  fi

  if ! err=$(git -C "$REPO_DIR" pull --ff-only 2>&1); then
    err_one=$(printf '%s\n' "$err" | sed '/^[[:space:]]*$/d' | awk '{printf "%s%s", sep, $0; sep=" | "} END {print ""}')
    log "拉取失败: ${err_one}"
    return 1
  fi

  after=$(git -C "$REPO_DIR" rev-parse HEAD)
  if [[ "$before" == "$after" ]]; then
    log "已是最新 ${after:0:12}"
    return 0
  fi

  log "有更新 ${before:0:12} -> ${after:0:12}"
  commits=$(git -C "$REPO_DIR" log --oneline "${before}..${after}")
  while IFS= read -r line; do
    [[ -n "$line" ]] && log "提交 $line"
  done <<< "$commits"
  return 0
}

LOCK_FILE="${LOG_FILE}.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "已有一个 auto_git_pull 在运行（锁: $LOCK_FILE）" >&2
  exit 1
fi

if [[ "$ONCE" -eq 1 ]]; then
  pull_once
  exit $?
fi

log "开始轮询，间隔 ${INTERVAL} 秒，仓库 ${REPO_DIR}，远程 ${REMOTE}"
while true; do
  pull_once || true
  sleep "$INTERVAL"
done
