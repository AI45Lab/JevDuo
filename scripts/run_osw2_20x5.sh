#!/usr/bin/env bash
# OSW2 批量测评：20 题、5 并发（5 个 rjob 各跑一个交错分片）。
# 用法（建议 nohup 挂后台）：
#   nohup bash scripts/run_osw2_20x5.sh > osw2-20x5.log 2>&1 &
#   tail -f osw2-20x5.log
# 可用环境变量：
#   LIMIT（默认 20）/ SHARDS（默认 5）/ MAX_STEPS（默认 30）/ MAX_WALL（默认 1800）
#   TIMEOUT（整片 watch 上限，默认 7200）/ PREFIX（run-id 前缀，默认 osw2-20）
#   BENCHMARK（基准，默认 osworld2）/ TASKS（逗号分隔题号，空 = 走清单）/ ROUTE_POLICY（路由，空 = 用 config）
#   SLOW_MODEL（慢模型 API 名，默认用 config 的 default）/ FAST_MODEL（快模型 API 名）
#   PREFIX 建议带上模型组合（如 osw2-kimi-kev），demo 按 run 对比时一眼能看出谁在跑
# OSW2.1：BENCHMARK=osworld21 LIMIT=108 SHARDS=4 PREFIX=osw21-108 bash scripts/run_osw2_20x5.sh
# 单题：再设置 TASKS=004 LIMIT=1 SHARDS=1；ROUTE_POLICY=gui 可限定 GUI 路由。
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

LIMIT="${LIMIT:-20}"
SHARDS="${SHARDS:-5}"
MAX_STEPS="${MAX_STEPS:-30}"
MAX_WALL="${MAX_WALL:-1800}"
TIMEOUT="${TIMEOUT:-7200}"
BENCHMARK="${BENCHMARK:-osworld2}"
TASKS="${TASKS:-}"
PREFIX="${PREFIX:-osw2-20}"
SLOW_MODEL="${SLOW_MODEL:-}"
FAST_MODEL="${FAST_MODEL:-}"
MODEL_FLAGS=()
[ -n "$SLOW_MODEL" ] && MODEL_FLAGS+=(--slow-model "$SLOW_MODEL")
[ -n "$FAST_MODEL" ] && MODEL_FLAGS+=(--fast-model "$FAST_MODEL")
BATCH_TS="$(date +%Y%m%d-%H%M%S)"
LOG_DIR="rjob_logs/${PREFIX}x${SHARDS}-${BATCH_TS}"
mkdir -p "$LOG_DIR"

echo "[$(date '+%F %T')] 批次启动: ${LIMIT} 题 / ${SHARDS} 并发 → 日志目录 ${LOG_DIR}"

pids=()
for i in $(seq 1 "$SHARDS"); do
  EXTRA=()
  if [ -n "$TASKS" ]; then
    EXTRA+=(--tasks "$TASKS")
  fi
  if [ -n "${ROUTE_POLICY:-}" ]; then
    EXTRA+=(--route-policy "$ROUTE_POLICY")
  fi
  # 只有第 1 片做 rsync：并发 --delete 同步会互删部署副本
  if [ "$i" -gt 1 ]; then
    EXTRA+=(--no-sync)
  fi
  echo "[$(date '+%F %T')] 提交分片 $i/$SHARDS (run-id ${PREFIX}-s${i})"
  jevduo-eval rjob run \
    --run-id "${PREFIX}-s${i}" \
    --benchmark "$BENCHMARK" \
    --limit "$LIMIT" --shard "$i/$SHARDS" \
    --max-steps "$MAX_STEPS" --max-wall "$MAX_WALL" \
    --repo . --config infra/config.json \
    --poll 60 --timeout "$TIMEOUT" \
    "${EXTRA[@]}" "${MODEL_FLAGS[@]}" \
    > "$LOG_DIR/shard-${i}.log" 2>&1 &
  pids+=($!)
  # 错开提交，给第 1 片的 rsync 留出完成窗口
  if [ "$i" -eq 1 ]; then
    echo "[$(date '+%F %T')] 等待分片 1 完成代码同步（rsync）…"
    sleep 90
  else
    sleep 5
  fi
done

echo "[$(date '+%F %T')] 全部 ${SHARDS} 片已提交，等待收敛（pid: ${pids[*]}）"
FAILED=0
for idx in "${!pids[@]}"; do
  pid=${pids[$idx]}
  shard=$((idx + 1))
  if wait "$pid"; then
    echo "[$(date '+%F %T')] 分片 $shard: 成功"
  else
    rc=$?
    echo "[$(date '+%F %T')] 分片 $shard: 失败（退出码 $rc，详见 $LOG_DIR/shard-$shard.log）"
    FAILED=$((FAILED + 1))
  fi
done

echo "[$(date '+%F %T')] 批次结束: $((SHARDS - FAILED))/${SHARDS} 片成功；汇总："
python3 - <<PYEOF
import json
from pathlib import Path

results_root = Path("rjob_logs")
total = scored = failed = 0
rows = []
for shard_dir in sorted(results_root.glob("${PREFIX}-s*")):
    summary = shard_dir / "summary.json"
    if not summary.is_file():
        rows.append((shard_dir.name, "-", "-", "无 summary"))
        continue
    data = json.loads(summary.read_text(encoding="utf-8"))
    total += data["total"]; scored += data["scored"]; failed += data["failed_unscored"]
    for task in data.get("tasks", []):
        rows.append((shard_dir.name, task["task_id"], task.get("score"), task.get("status")))
print(f"总计 {total} 题 | scored {scored} | failed_unscored {failed}")
for row in rows:
    print("  " + " | ".join(str(x) for x in row))
PYEOF

exit "$FAILED"
