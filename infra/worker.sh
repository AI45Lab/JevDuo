#!/usr/bin/env bash
# rjob worker 入口：准备共享环境与 docker，再进入 entry。
set -euo pipefail

SPEC="${1:?用法: worker.sh <job spec json 绝对路径>}"
[ -f "$SPEC" ] || { echo "job spec 不存在: $SPEC" >&2; exit 4; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT/src:$ROOT/src/upstream${PYTHONPATH:+:$PYTHONPATH}"

get() { python3 -c 'import json,sys
node=json.load(open(sys.argv[1]))
for key in sys.argv[2].split("."):
    node=node[key] if isinstance(node,dict) else None
print("" if node is None else node)' "$SPEC" "$1"; }

OUTPUT_ROOT="$(get output_root)"
BENCHMARK="$(get benchmark.name)"
SHARED_ENV="${SA_CUA_SHARED_ENV:-$(get benchmark.shared_env)}"
mkdir -p "$OUTPUT_ROOT/worker"
LOG="$OUTPUT_ROOT/worker/entry.log"
[ "$BENCHMARK" = "osworld21" ] || { echo "本工程只支持 osworld21，收到: $BENCHMARK" | tee "$LOG" >&2; exit 4; }
[ -f "$SHARED_ENV" ] || { echo "缺少共享资源 env.sh: $SHARED_ENV" | tee "$LOG" >&2; exit 3; }

# shellcheck disable=SC1090
source "$SHARED_ENV"
python3 "$OSWORLD21_ROOT/setup/shared_services.py" hosts

mkdir -p "$HOME/.docker"
# spec 里已是 base64(user:pass) 的字面值（settings.py 渲染时做过归一化），直接写 docker config
AUTH="$(python3 -c 'import json,sys
spec=json.load(open(sys.argv[1]))
auths={host:{"auth":entry["auth"]}
       for host,entry in ((spec.get("cluster") or {}).get("registry_auth") or {}).items()
       if entry.get("auth")}
print(json.dumps({"auths":auths}) if auths else "")
' "$SPEC")"
if [ -n "$AUTH" ]; then
  printf '%s' "$AUTH" > "$HOME/.docker/config.json"
  chmod 600 "$HOME/.docker/config.json"
fi

INNER_IMAGE="$(get benchmark.inner_image)"
PROXY="$(get benchmark.proxy)"
PROXY="${PROXY:-http://httpproxy-headless.kubebrain.svc.pjlab.local:3128}"
export HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" http_proxy="$PROXY" https_proxy="$PROXY"
NO_PROXY="10.0.0.0/8,100.96.0.0/12,localhost,127.0.0.1,.pjlab.org.cn${NO_PROXY:+,$NO_PROXY}"
export NO_PROXY no_proxy="$NO_PROXY"

DOCKER_ROOT="$(mktemp -d /tmp/jevduo-docker.XXXXXX)"
dockerd --host=unix:///var/run/docker.sock --data-root="$DOCKER_ROOT/data" \
  --exec-root="$DOCKER_ROOT/exec" --http-proxy="$PROXY" --https-proxy="$PROXY" \
  --no-proxy="$NO_PROXY" > "$OUTPUT_ROOT/worker/dockerd.log" 2>&1 &
for _ in $(seq 1 90); do
  docker info >/dev/null 2>&1 && break
  sleep 2
done
docker info > "$OUTPUT_ROOT/worker/docker-info.txt" 2>&1 || { echo "dockerd 未就绪" | tee -a "$LOG" >&2; exit 3; }
docker pull "$INNER_IMAGE" > "$OUTPUT_ROOT/worker/docker-pull.log" 2>&1 || {
  tail -20 "$OUTPUT_ROOT/worker/docker-pull.log" | tee -a "$LOG" >&2
  echo "inner 镜像拉取失败: $INNER_IMAGE" | tee -a "$LOG" >&2; exit 3; }

"$OSWORLD_PYTHON" -c 'import PIL' > "$OUTPUT_ROOT/worker/deps-check.log" 2>&1 || {
  tail -20 "$OUTPUT_ROOT/worker/deps-check.log" | tee -a "$LOG" >&2
  echo "共享 venv 依赖检查失败" | tee -a "$LOG" >&2; exit 3; }
env -u PYTHONPATH -u LD_LIBRARY_PATH "$ROOT/venv/bin/python" -c 'import northcua, nexau' \
  >> "$OUTPUT_ROOT/worker/deps-check.log" 2>&1 || {
  tail -20 "$OUTPUT_ROOT/worker/deps-check.log" | tee -a "$LOG" >&2
  echo "agent venv 导入失败（检查 $ROOT/venv 与 $ROOT/src/upstream 在 worker 内是否可见）" | tee -a "$LOG" >&2
  exit 3; }

"$OSWORLD_PYTHON" -m entry --spec "$SPEC" 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
