#!/usr/bin/env bash
# rjob worker 内入口：读取 job spec，进入 jevduo_eval entry。
# - mock 基准：直接用系统 python3 跑（无需共享资源/docker）。
# - osworld1/2/21：加载共享资源 env.sh（冻结 venv/评测工具/JEV），启动 dockerd 并拉取
#   inner 镜像（VM 容器），然后用 OSWORLD_PYTHON 跑 entry（Pillow 等依赖已在冻结 venv 内）。
set -euo pipefail

SPEC="${1:?用法: worker.sh <job spec json 绝对路径>}"
if [ ! -f "$SPEC" ]; then
  echo "job spec 不存在: $SPEC —— 通常因并发 run 的 rsync --delete 删掉了在跑任务的 spec" \
    "（已修复：job_specs 现被排除出同步）；请重跑该 run-id" >&2
  exit 4
fi
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

# 从 job spec 取点号路径的值（缺失 → 空串）
spec_get() {
  python3 -c '
import json, sys
node = json.load(open(sys.argv[1]))
for part in sys.argv[2].split("."):
    if not isinstance(node, dict):
        node = None
        break
    node = node.get(part)
print("" if node is None else node)' "$SPEC" "$1"
}

read -r BENCHMARK OUTPUT_ROOT < <(python3 -c '
import json, sys
spec = json.load(open(sys.argv[1]))
print(spec.get("benchmark", "mock"), spec["output_root"])' "$SPEC")
mkdir -p "$OUTPUT_ROOT/worker"

if [ "$BENCHMARK" = "mock" ]; then
  python3 -m jevduo_eval entry --spec "$SPEC" 2>&1 | tee "$OUTPUT_ROOT/worker/entry.log"
  exit "${PIPESTATUS[0]}"
fi

# ---- 真实基准：共享资源 + docker 准备 ----
# shared_env 由 spec 给出（OSW1/OSW2 用旧环境的 env.sh，OSW2.1 用它自己的独立 env.sh）；
# SA_CUA_SHARED_ENV 可临时覆盖。
SHARED_ENV="${SA_CUA_SHARED_ENV:-$(spec_get env.shared_env)}"
if [ ! -f "$SHARED_ENV" ]; then
  echo "缺少共享资源 env.sh: $SHARED_ENV" | tee "$OUTPUT_ROOT/worker/entry.log" >&2
  exit 3
fi
# shellcheck disable=SC1090
source "$SHARED_ENV"
if [ "$BENCHMARK" = "osworld21" ]; then
  python3 "$OSWORLD21_ROOT/setup/shared_services.py" hosts
fi

mkdir -p "$HOME/.docker"
python3 -c '
import json, sys, pathlib
registry = json.load(open(sys.argv[1]))["secrets"].get("registry")
if not registry:
    print("缺少 registry 凭据（config.json 的 registry 段为空）", file=sys.stderr); raise SystemExit(3)
pathlib.Path.home().joinpath(".docker/config.json").write_text(json.dumps(registry))' "$SPEC" || {
  cat "$OUTPUT_ROOT/worker/entry.log" 2>/dev/null || true
  echo "registry 凭据写入失败" | tee -a "$OUTPUT_ROOT/worker/entry.log" >&2; exit 3; }
chmod 600 "$HOME/.docker/config.json"

INNER_IMAGE="$(spec_get code.images.inner)"
# 平台可能预注入不可用的代理（真机 2026-10-20：httpproxy-headless-dev-test 无法解析），
# 一律覆盖为已知可用代理，不继承环境变量。
PROXY="$(spec_get env.proxy)"
PROXY="${PROXY:-http://httpproxy-headless.kubebrain.svc.pjlab.local:3128}"
export HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" http_proxy="$PROXY" https_proxy="$PROXY"
if [ "$BENCHMARK" = "osworld21" ]; then
  OSWORLD21_NO_PROXY="$(spec_get env.no_proxy)"
  export NO_PROXY="10.0.0.0/8,100.96.0.0/12,localhost,127.0.0.1,.pjlab.org.cn${NO_PROXY:+,$NO_PROXY}${OSWORLD21_NO_PROXY:+,$OSWORLD21_NO_PROXY}"
else
  export NO_PROXY='10.0.0.0/8,100.96.0.0/12,localhost,127.0.0.1,.pjlab.org.cn'
fi
export no_proxy="$NO_PROXY"

DOCKER_ROOT="$(mktemp -d /tmp/jevduo-docker.XXXXXX)"
dockerd --host=unix:///var/run/docker.sock \
  --data-root="$DOCKER_ROOT/data" --exec-root="$DOCKER_ROOT/exec" \
  --http-proxy="$PROXY" --https-proxy="$PROXY" --no-proxy="$NO_PROXY" \
  > "$OUTPUT_ROOT/worker/dockerd.log" 2>&1 &
for _ in $(seq 1 90); do
  docker info >/dev/null 2>&1 && break
  sleep 2
done
docker info > "$OUTPUT_ROOT/worker/docker-info.txt" 2>&1 || {
  echo "dockerd 未就绪" | tee -a "$OUTPUT_ROOT/worker/entry.log" >&2; exit 3; }
docker pull "$INNER_IMAGE" > "$OUTPUT_ROOT/worker/docker-pull.log" 2>&1 || {
  tail -20 "$OUTPUT_ROOT/worker/docker-pull.log" | tee -a "$OUTPUT_ROOT/worker/entry.log" >&2
  echo "inner 镜像拉取失败: $INNER_IMAGE" | tee -a "$OUTPUT_ROOT/worker/entry.log" >&2; exit 3; }
if [ "$BENCHMARK" = "osworld21" ]; then
  # OSW2.1 的独立 venv 按官方 uv.lock 装，没有 easyocr；只查 Pillow
  DEPS_CHECK='import PIL'
else
  docker tag "$INNER_IMAGE" happysixd/osworld-docker:latest
  DEPS_CHECK='import easyocr, PIL; assert callable(easyocr.Reader)'
fi

"$OSWORLD_PYTHON" -c "$DEPS_CHECK" \
  > "$OUTPUT_ROOT/worker/deps-check.log" 2>&1 || {
  tail -20 "$OUTPUT_ROOT/worker/deps-check.log" | tee -a "$OUTPUT_ROOT/worker/entry.log" >&2
  echo "共享 venv 依赖检查失败" | tee -a "$OUTPUT_ROOT/worker/entry.log" >&2; exit 3; }

"$OSWORLD_PYTHON" -m jevduo_eval entry --spec "$SPEC" 2>&1 | tee "$OUTPUT_ROOT/worker/entry.log"
exit "${PIPESTATUS[0]}"
