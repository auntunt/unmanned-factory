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

echo "==> 产物清单"
ls -la "$OUT" | head -10
du -sh "$OUT"
