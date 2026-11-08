#!/usr/bin/env bash
# JevDuo 集群测评唯一入口
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CONFIG="$ROOT/config.yaml"; RUN_ID=""; TASKS=""; LIMIT=""
SHARD=""; JOBS=""; PER_JOB=""; MODEL=""; MAX_STEPS=""; MAX_WALL=""; KEEP=0
DETACH=0; RESUME=1; RETRY_FAILED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --run-id)  RUN_ID="$2";  shift 2 ;;
    --tasks)   TASKS="$2";   shift 2 ;;
    --limit)   LIMIT="$2";   shift 2 ;;
    --shard)   SHARD="$2";   shift 2 ;;
    --jobs)    JOBS="$2";    shift 2 ;;
    --per-job) PER_JOB="$2"; shift 2 ;;
    --model)   MODEL="$2";   shift 2 ;;
    --max-steps) MAX_STEPS="$2"; shift 2 ;;
    --max-wall)  MAX_WALL="$2";  shift 2 ;;
    --config)  CONFIG="$2";  shift 2 ;;
    --keep)    KEEP=1; shift ;;
    --detach)  DETACH=1; shift ;;
    --no-resume) RESUME=0; shift ;;
    --retry-failed) RETRY_FAILED=1; shift ;;
    -h|--help) sed -n '2p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done
[ -n "$RUN_ID" ] || {
  echo "用法: bash scripts/run.sh --run-id NAME [--tasks 001,002] [--shard I/N] [--jobs N] [--per-job K]" >&2
  echo "       [--model NAME] [--max-steps N] [--max-wall S] [--keep] [--detach]" >&2
  echo "       [--no-resume] [--retry-failed]" >&2
  exit 2
}

LOG_DIR="$ROOT/runs/$RUN_ID"; mkdir -p "$LOG_DIR"; rm -f "$LOG_DIR/submit.log"
export PYTHONPATH="$ROOT/src"

ARGS=(--config "$CONFIG" --run-id "$RUN_ID")
[ -n "$TASKS" ]   && ARGS+=(--tasks "$TASKS")
[ -n "$LIMIT" ]   && ARGS+=(--limit "$LIMIT")
[ -n "$SHARD" ]   && ARGS+=(--shard "$SHARD")
[ -n "$JOBS" ]    && ARGS+=(--jobs "$JOBS")
[ -n "$PER_JOB" ] && ARGS+=(--per-job "$PER_JOB")
[ -n "$MODEL" ]     && ARGS+=(--model "$MODEL")
[ -n "$MAX_STEPS" ] && ARGS+=(--max-steps "$MAX_STEPS")
[ -n "$MAX_WALL" ]  && ARGS+=(--max-wall "$MAX_WALL")
ARGS+=(--resume "$RESUME" --retry-failed "$RETRY_FAILED")
[ -f "$ROOT/.env.local" ] && set -a && . "$ROOT/.env.local" && set +a

# 常驻模式：脱离当前终端/会话，会话结束不连带杀死长测评
if [ "$DETACH" = "1" ]; then
  PID_FILE="$LOG_DIR/pool.pid"
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "run $RUN_ID 已有 pool 在跑（pid $(cat "$PID_FILE")）；要再跑请先停掉它" >&2
    exit 3
  fi
  setsid nohup "$ROOT/venv/bin/python" -m pool "${ARGS[@]}" --keep "$KEEP" \
    > "$LOG_DIR/pool.log" 2>&1 < /dev/null &
  echo $! > "$PID_FILE"
  sleep 1
  echo "已后台启动 run=$RUN_ID pid=$(cat "$PID_FILE")"
  echo "日志: $LOG_DIR/pool.log"
  echo "停止: kill \$(cat $PID_FILE)"
  exit 0
fi

exec "$ROOT/venv/bin/python" -m pool "${ARGS[@]}" --keep "$KEEP" 2>&1 | tee "$LOG_DIR/submit.log"
