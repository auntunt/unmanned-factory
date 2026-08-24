#!/usr/bin/env bash
# 构建前端到仓库外，然后清掉仓库内的 dist。
#
# 为什么产物不留在仓库里：factory/harness/workspace.py 的 shadow_code() 会把
# 被 .gitignore 挡住的代码文件（含 frontend/dist/*.js）判为「影子代码」——
# 那道闸门是防 worker 往 ignored 目录塞代码逃过 diff 审查的，dist/ 刻意不在
# 豁免表里（见该文件 :171 的注释）。所以正解是产物别落在工作树，而不是放宽闸门。
#
# 用法：bash deploy/build-frontend.sh [目标目录]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-/home/ubuntu/factory-web/dist}"
# Caddy 实际托管的目录（见下面「同步」一段说明为什么不能直接托管 $OUT）
SERVE="${2:-/srv/factory-web}"

cd "$REPO/frontend"

echo "==> 构建（产物 → $OUT）"
mkdir -p "$OUT"
npx vite build --outDir "$OUT" --emptyOutDir

echo "==> 确认仓库内无残留 dist"
rm -rf "$REPO/frontend/dist"

echo "==> 影子代码闸门自检"
cd "$REPO"
uv run python -c "
from pathlib import Path
from factory.harness.workspace import shadow_code
hits = shadow_code(Path('.'))
assert hits == (), f'影子代码闸门命中 {len(hits)} 条: {hits[:5]}'
print('  闸门 0 命中 ✓')
" 2>&1 | grep -v '^warning:'

echo "==> 同步到 Caddy 可读目录"
# /home/ubuntu 是 750，caddy 用户进不去 —— 直接 root 到 $OUT 会全站 403。
# 放宽家目录影响面太大，所以产物复制到 /srv 下由 caddy 组读。
# 跳过这步的话线上还是旧版本，看不出报错，只是页面不更新。
if [ -d "$SERVE" ] || sudo -n true 2>/dev/null; then
  sudo mkdir -p "$SERVE"
  sudo rsync -a --delete "$OUT/" "$SERVE/"
  sudo chown -R root:caddy "$SERVE"
  sudo chmod -R a+rX "$SERVE"
  sudo -u caddy test -r "$SERVE/index.html" \
    && echo "  caddy 可读 $SERVE ✓" \
    || { echo "  ✗ caddy 读不到 $SERVE，线上不会更新"; exit 1; }
else
  echo "  ⚠ 没有 sudo，跳过同步 —— 线上仍是旧版本"
fi

echo "==> 产物清单"
ls -la "$OUT" | head -10
du -sh "$OUT"
