#!/usr/bin/env bash
# Updates dependencies/assets as the service account. Existing systemd, Caddy,
# credentials and databases are preserved. No automatic activation.
set -Eeuo pipefail
usage() {
  cat <<'HELP'
Usage: install-control.sh --checkout PATH --user USER [options]
  --env-file PATH   Existing protected external EnvironmentFile (required)
  --data-dir PATH   Existing FACTORY_CONTROL_DATA directory (required)
  --unit-file PATH  Existing unit (default /etc/systemd/system/factoryweb.service)
  --apply          Sync/build and run local doctor; default is dry-run
  --dry-run        Print the plan without modifying files
This updater never installs/replaces units or starts/restarts services. For a
new host, review the factory-control.service example separately.
HELP
}
CHECKOUT=""
SERVICE_USER=""
ENV_FILE=""
DATA_DIR=""
UNIT_FILE="/etc/systemd/system/factoryweb.service"
APPLY=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --checkout) CHECKOUT=${2:?}; shift 2 ;;
    --user) SERVICE_USER=${2:?}; shift 2 ;;
    --env-file) ENV_FILE=${2:?}; shift 2 ;;
    --data-dir) DATA_DIR=${2:?}; shift 2 ;;
    --unit-file) UNIT_FILE=${2:?}; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option" >&2; usage >&2; exit 2 ;;
  esac
done
[[ -n "$CHECKOUT" && -n "$SERVICE_USER" && -n "$ENV_FILE" && -n "$DATA_DIR" ]] || { usage >&2; exit 2; }
[[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*\$?$ ]] || { echo "Invalid service user" >&2; exit 2; }
for value in "$CHECKOUT" "$ENV_FILE" "$DATA_DIR" "$UNIT_FILE"; do
  [[ "$value" == /* && "$value" != *$'\n'* && "$value" != *$'\r'* ]] || { echo "Paths must be absolute and single-line" >&2; exit 2; }
done
CHECKOUT=$(cd "$CHECKOUT" && pwd -P)
[[ "$(git -C "$CHECKOUT" rev-parse --show-toplevel)" == "$CHECKOUT" ]] || { echo "Expected Git worktree root" >&2; exit 1; }
[[ -f "$CHECKOUT/frontend/package-lock.json" && -f "$CHECKOUT/uv.lock" ]] || { echo "Dependency lockfiles required" >&2; exit 1; }
for binary in uv node npm git; do
  command -v "$binary" >/dev/null || { echo "Missing executable: $binary" >&2; exit 1; }
done
[[ -f "$ENV_FILE" && -f "$UNIT_FILE" && -d "$DATA_DIR" ]] || { echo "Existing env file, unit and data directory required; use the new-host guide for first installation" >&2; exit 1; }
mode=$(stat -c '%a' "$ENV_FILE")
(( (8#$mode & 077) == 0 )) || { echo "Environment file must have owner-only permissions" >&2; exit 1; }
SERVICE_NAME=$(basename "$UNIT_FILE")
[[ "$SERVICE_NAME" =~ ^[a-zA-Z0-9_-]+\.service$ ]] || { echo "Invalid unit filename" >&2; exit 1; }
case "$SERVICE_NAME" in factoryapi.service|factory-api.service|factory-dashboard.service|factory-worker.service)
  echo "Legacy service cannot be used for v2" >&2; exit 1 ;;
esac
printf 'Checkout: %s\nService: %s\nEnvironment: %s (values not read)\nData: %s\n' "$CHECKOUT" "$SERVICE_NAME" "$ENV_FILE" "$DATA_DIR"
if (( APPLY )); then
  [[ "$(id -un)" == "$SERVICE_USER" ]] || { echo "Run --apply as the service account; do not build as root" >&2; exit 1; }
  (cd "$CHECKOUT" && uv sync --frozen --all-extras --no-dev)
  (cd "$CHECKOUT/frontend" && npm ci)
  (cd "$CHECKOUT/frontend" && node ./node_modules/vite/bin/vite.js build)
  "$CHECKOUT/.venv/bin/python" -m factory.control.runtime_cli doctor --json \
    --workspace "$CHECKOUT" --static-dir "$CHECKOUT/frontend/dist" --db "$DATA_DIR/control.db"
else
  printf '%s\n' 'DRY-RUN: no changes' \
    'As the service account, in the checkout: uv sync --frozen --all-extras --no-dev' \
    'In frontend: npm ci' 'In frontend: node ./node_modules/vite/bin/vite.js build' \
    'Run .venv/bin/python -m factory.control.runtime_cli doctor with the paths above.'
fi
printf '%s\n' 'Existing service, EnvironmentFile, Caddy, and live database remain intact.' \
  'Doctor uses the CLI environment. Verify actual runtime settings through the authenticated service.'
printf 'After checking the installed revision and build, activate the existing unit: sudo systemctl restart %s\n' "$SERVICE_NAME"
printf '%s\n' 'Never enable --now a second service on port 8788. Do not activate legacy factoryapi/factory-api.'
