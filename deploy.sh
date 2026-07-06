#!/bin/bash
# deploy.sh — thin shim (Phase 2 標準型)。
# 所有 deploy 行為住 deploy.manifest.json，由共用 wsdeploy engine 執行
# （cathyyul/workspace-infra；guards：ownership／orphan-edit／source-hygiene／lockfile）。
# 不要在本檔加邏輯——加部署目標＝改 manifest；非宣告式長尾＝manifest hooks。
#
# Engine 解析順序（bootstrap 不循環）：
#   1. $WSDEPLOY（env seam）
#   2. <本 repo>/bin/wsdeploy（workspace-infra self-hosting：engine 就在 repo 內）
#   3. $WORKSPACE/scripts/wsdeploy（一般情況：workspace 部署版）
#   4. workspace-infra checkout 的 bin/wsdeploy（workspace 版遺失時的復原路徑）
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
WS="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}"
for c in "${WSDEPLOY:-}" \
         "$REPO_DIR/bin/wsdeploy" \
         "$WS/scripts/wsdeploy" \
         "$WS/out/daily-review/projects/workspace-infra/bin/wsdeploy"; do
  if [ -n "$c" ] && [ -x "$c" ]; then
    exec "$c" --manifest "$REPO_DIR/deploy.manifest.json" "$@"
  fi
done
echo "deploy: wsdeploy engine not found（查過：\$WSDEPLOY、$REPO_DIR/bin/、$WS/scripts/、infra checkout）" >&2
echo "  bootstrap：clone https://github.com/cathyyul/workspace-infra 到" >&2
echo "  $WS/out/daily-review/projects/workspace-infra，跑它的 deploy.sh（engine 自帶於 repo bin/），再重跑本 deploy。" >&2
exit 1
