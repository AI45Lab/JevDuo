#!/usr/bin/env bash
# rjob worker 入口：准备共享环境与 docker，再进入 entry。
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

SPEC="${1:?用法: worker.sh <job spec json 绝对路径>}"
[ -f "$SPEC" ] || { echo "job spec 不存在: $SPEC" >&2; exit 4; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 基础镜像会往 PYTHONPATH 里塞 /opt/osworld-v2 与 /opt/weavebench：前者是旧版
# desktop_env（SetupController.execute 没有 check 参数），会把共享 runtime 顶掉，
# 表现为 task setup 报 "unexpected keyword argument 'check'"；后者会在解释器启动期
# 注入路径（sitecustomize），同样能把旧副本带回来。这里两条一并摘掉。
cleanup_pythonpath() {
  local kept="" entry
  local IFS=':'
  for entry in $1; do
    case "$entry" in
      /opt/osworld-v2|/opt/weavebench) continue ;;
    esac
    kept="${kept:+$kept:}$entry"
  done
  printf '%s' "$kept"
}
PYTHONPATH="$(cleanup_pythonpath "${PYTHONPATH:-}")"
export PYTHONPATH="$ROOT/src:$ROOT/src/upstream${PYTHONPATH:+:$PYTHONPATH}"

# 基础镜像的 WORKDIR 就是 /opt/osworld-v2，而 python 默认把「当前目录」放在
# sys.path[0]，那份旧 desktop_env（SetupController.execute 没有 check 参数）就会
# 抢先命中，表现为 task setup 报 "unexpected keyword argument 'check'"。
# 两道保险：切到仓库目录运行 + 禁止把 cwd 注入 sys.path。
cd "$ROOT"
export PYTHONSAFEPATH=1

get() { python3 -c 'import json,sys
node=json.load(open(sys.argv[1]))
for key in sys.argv[2].split("."):
    node=node[key] if isinstance(node,dict) else None
print("" if node is None else node)' "$SPEC" "$1"; }

OUTPUT_ROOT="$(get output_root)"
BENCHMARK="$(get benchmark.name)"
SHARED_ENV="${SA_CUA_SHARED_ENV:-$(get benchmark.shared_env)}"
GUI_BACKEND="$(python3 -c 'import json,sys
print(json.load(open(sys.argv[1]))["harness"].get("local_gui", {}).get("backend", "native"))' "$SPEC")"
if [ "$GUI_BACKEND" = jev ]; then
  if [ "${2:-}" != --private-storage ]; then
    exec unshare --mount --propagation private bash "$0" "$SPEC" --private-storage
  fi
  if [ "$(findmnt -n -o FSTYPE -T "$OUTPUT_ROOT")" != gpfs ] || \
     [ "$(stat -f -c %T /tmp)" != tmpfs ] || [ "$(wc -l < /proc/swaps)" -ne 1 ]; then
    echo "storage_policy_failed: JEV requires GPFS results and unswapped tmpfs; no local-disk fallback" >&2
    exit 42
  fi
  # Only this child mount namespace changes; no host files or block devices are mounted or written.
  mount -t tmpfs -o size=64m,nosuid,nodev tmpfs /run
  storage_hosts_file="$(mktemp /tmp/jevduo-hosts.XXXXXX)"
  cp /etc/hosts "$storage_hosts_file"
  mount --bind "$storage_hosts_file" /etc/hosts
  export DOCKER_CONFIG="$OUTPUT_ROOT/worker/docker-config"
  export XDG_CACHE_HOME="$OUTPUT_ROOT/worker/cache"
  export TMPDIR=/tmp
fi
mkdir -p "$OUTPUT_ROOT/worker"
LOG="$OUTPUT_ROOT/worker/entry.log"
[ "$BENCHMARK" = "osworld21" ] || { echo "本工程只支持 osworld21，收到: $BENCHMARK" | tee "$LOG" >&2; exit 4; }
[ -f "$SHARED_ENV" ] || { echo "缺少共享资源 env.sh: $SHARED_ENV" | tee "$LOG" >&2; exit 3; }

# shellcheck disable=SC1090
source "$SHARED_ENV"
python3 "$OSWORLD21_ROOT/setup/shared_services.py" hosts

REGISTRY_CONFIG_DIR="${DOCKER_CONFIG:-$HOME/.docker}"
mkdir -p "$REGISTRY_CONFIG_DIR"
# spec 里已是 base64(user:pass) 的字面值（settings.py 渲染时做过归一化），直接写 docker config
AUTH="$(python3 -c 'import json,sys
spec=json.load(open(sys.argv[1]))
auths={host:{"auth":entry["auth"]}
       for host,entry in ((spec.get("cluster") or {}).get("registry_auth") or {}).items()
       if entry.get("auth")}
print(json.dumps({"auths":auths}) if auths else "")
' "$SPEC")"
if [ -n "$AUTH" ]; then
  printf '%s' "$AUTH" > "$REGISTRY_CONFIG_DIR/config.json"
  chmod 600 "$REGISTRY_CONFIG_DIR/config.json"
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
# 记录 desktop_env 的实际解析位置与接口签名：历史上有过 /opt/osworld-v2 覆盖共享
# runtime 的情况（task 024 的 setup 报 unexpected keyword 'check'），出问题时先看这里。
"$OSWORLD_PYTHON" - >> "$OUTPUT_ROOT/worker/deps-check.log" 2>&1 <<'PY' || true
import inspect
import os
import sys

import desktop_env
from desktop_env.controllers.setup import SetupController

print("desktop_env:", desktop_env.__file__)
print("SetupController.execute:", inspect.signature(SetupController.execute))
print("OSWORLD2_ROOT:", os.environ.get("OSWORLD2_ROOT"))
print("PYTHONPATH:", os.environ.get("PYTHONPATH"))
print("cwd:", os.getcwd())
print("sys.path[0:2]:", repr(sys.path[0]), repr(sys.path[1]) if len(sys.path) > 1 else "-")
for index, entry in enumerate(sys.path):
    if entry == "" or "osworld" in entry or entry.startswith(("/opt", "/mnt")):
        print(f"sys.path[{index}]: {entry!r}")
print("meta_path:", [type(finder).__name__ for finder in sys.meta_path])
PY
env -u PYTHONPATH -u LD_LIBRARY_PATH "$ROOT/venv/bin/python" -c 'import northcua, nexau' \
  >> "$OUTPUT_ROOT/worker/deps-check.log" 2>&1 || {
  tail -20 "$OUTPUT_ROOT/worker/deps-check.log" | tee -a "$LOG" >&2
  echo "agent venv 导入失败（检查 $ROOT/venv 与 $ROOT/src/upstream 在 worker 内是否可见）" | tee -a "$LOG" >&2
  exit 3; }

# 基础镜像的工作目录就是 /opt/osworld-v2，而 python 会把「当前目录」放在 sys.path[0]，
# 于是里面那份旧 desktop_env（SetupController.execute 没有 check 参数）总会先命中，
# 表现为 task setup 报 "unexpected keyword argument 'check'"。防止见脚本开头。

"$OSWORLD_PYTHON" -m entry --spec "$SPEC" 2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
