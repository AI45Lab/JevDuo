#!/usr/bin/env bash
# 重建 agent venv：源码在 src/upstream/，venv 在仓库根 venv/。
# editable 安装的 .pth 写绝对路径，源码搬家后必须重建。
# 只在开发机/换机时跑一次；worker 侧用冻结的 OSWORLD_PYTHON，不碰这里。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM="$ROOT/src/upstream"
VENV="$ROOT/venv"

[ -f "$UPSTREAM/pyproject.toml" ] || { echo "缺少 $UPSTREAM/pyproject.toml（src/upstream 应由 git subtree 提供）" >&2; exit 2; }

# 上游 uv.lock 要求 uv>=0.13.6；系统 uv 常偏旧，按候选顺序挑第一个满足的。
UV_REQUIRED=0.13.6
uv_ok() { "$1" --version 2>/dev/null | awk -v need="$UV_REQUIRED" '{
    split($2, v, "."); split(need, n, ".");
    if (v[1] > n[1]) exit 0;
    if (v[1] == n[1] && v[2] > n[2]) exit 0;
    if (v[1] == n[1] && v[2] == n[2] && v[3] >= n[3]) exit 0;
    exit 1 }'; }
UV=""
for candidate in "${JEVDUO_UV:-}" "$ROOT/tools/uv-venv/bin/uv" \
                 /mnt/shared-storage-user/adpc-share/chenxinquan/fastslow/northcua-osw21/tools/uv-venv/bin/uv \
                 "$(command -v uv 2>/dev/null || true)"; do
  [ -n "$candidate" ] && [ -x "$candidate" ] && uv_ok "$candidate" && { UV="$candidate"; break; }
done
[ -n "$UV" ] || { echo "找不到 uv>=$UV_REQUIRED（可用 JEVDUO_UV 指定）" >&2; exit 3; }
echo "uv: $UV ($("$UV" --version))"

# 上游快照不带 .git；uv 对 git 依赖需要 VCS 信息时不至于卡住。
# 解释器必须装在共享盘：worker 容器里看不到开发机的 ~/.local，venv 的 python 是
# 指向解释器的符号链接，指向本地路径会直接 No such file or directory。
export UV_PROJECT_ENVIRONMENT="$VENV"
export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_INSTALL_DIR:-$ROOT/python}"
export UV_LINK_MODE=copy

rm -rf "$VENV"
"$UV" python install 3.12

cd "$UPSTREAM"
"$UV" sync --locked --python 3.12 --extra local --extra device

"$VENV/bin/python" - <<'PY'
import northcua, nexau, yaml, sys
print("northcua:", northcua.__file__)
print("nexau:", nexau.__file__)
print("yaml:", yaml.__version__)
print("sys.executable:", sys.executable)
PY
INTERP="$(readlink -f "$VENV/bin/python")"
case "$INTERP" in
  "$ROOT"/*) echo "interpreter on shared storage: $INTERP" ;;
  *) echo "解释器不在共享盘（worker 内不可见）: $INTERP" >&2; exit 4 ;;
esac
echo SETUP_OK
