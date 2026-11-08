#!/usr/bin/env bash
# 把上游 CUA 源码同步到 src/upstream/（只读快照，保留原包名）。
# 上游仓库位置可用 JEVDUO_UPSTREAM_SRC 覆盖，默认是同级目录下的 NorthCUA。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${JEVDUO_UPSTREAM_SRC:-/mnt/shared-storage-user/adpc-share/chenxinquan/fastslow/NorthCUA}"
DST="$ROOT/src/upstream"

[ -d "$SRC/northcua" ] || { echo "上游源码不在 $SRC（可用 JEVDUO_UPSTREAM_SRC 指定）" >&2; exit 2; }

# 四个顶层包 + 本地依赖包目录 + 打包配置；docs/examples/tests 不参与运行，不带。
mkdir -p "$DST"
for item in northcua northcua_protocol northcua_runtime_shared northcua_controller_plugins \
            packages config skills pyproject.toml uv.lock README.md LICENSE NOTICE; do
  [ -e "$SRC/$item" ] || continue
  rsync -a --delete \
    --exclude '.git/' --exclude '.venv/' --exclude '__pycache__/' \
    --exclude '*.egg-info/' --exclude '.pytest_cache/' --exclude '.ruff_cache/' \
    "$SRC/$item" "$DST/"
done

{
  echo "# 上游源码快照（由 scripts/sync_upstream.sh 生成，勿手改）"
  echo "source: $SRC"
  echo "synced_at: $(date -Iseconds)"
  git -C "$SRC" describe --always --dirty 2>/dev/null | sed 's/^/describe: /' || true
  git -C "$SRC" rev-parse HEAD 2>/dev/null | sed 's/^/commit: /' || true
} > "$DST/REVISION.txt"

du -sh "$DST"
tail -3 "$DST/REVISION.txt"
