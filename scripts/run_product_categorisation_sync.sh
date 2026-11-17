#!/usr/bin/env bash
# Daily cron wrapper for the product categorisation sync (Linux server twin of
# run_product_categorisation_sync.ps1).
#
#  * resolves the project directory from this script's own location;
#  * uses the project's virtualenv interpreter and passes .env explicitly
#    (python-dotenv reads it -- never sourced, so passwords with $ are safe);
#  * a non-blocking flock makes an overlapping run exit instead of waiting;
#  * exits with the Python process's status.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="${PC_SYNC_PYTHON:-$PROJECT_DIR/.venv/bin/python}"
ENV_FILE="${PC_SYNC_ENV_FILE:-$PROJECT_DIR/.env}"
LOCK_FILE="$PROJECT_DIR/data/cron.lock"

stamp() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) ($(TZ=Asia/Kolkata date +%H:%M) IST)"; }

[[ -x "$PYTHON" ]] || { echo "$(stamp) ERROR python interpreter not found: $PYTHON" >&2; exit 2; }
[[ -f "$ENV_FILE" ]] || { echo "$(stamp) ERROR env file not found: $ENV_FILE" >&2; exit 2; }

mkdir -p "$PROJECT_DIR/data" "$PROJECT_DIR/logs"
cd "$PROJECT_DIR" || exit 2

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "$(stamp) SKIPPED another product categorisation sync is still running (lock $LOCK_FILE)" >&2
    exit 11
fi

[[ $# -eq 0 ]] && set -- --sync
echo "$(stamp) START product categorisation sync ($*, pid $$)"
"$PYTHON" "$PROJECT_DIR/run.py" --env-file "$ENV_FILE" "$@"
status=$?
if [[ $status -eq 0 ]]; then
    echo "$(stamp) END status=0 SUCCESS"
else
    echo "$(stamp) END status=$status FAILED" >&2
fi
exit $status
