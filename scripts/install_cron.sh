#!/usr/bin/env bash
# Linux server: verify the deployment, then install (or print) the daily cron entry.
#
#   scripts/install_cron.sh           # verify + install into the current user's crontab
#   scripts/install_cron.sh --print   # verify + print the entry only
#
# Runs daily at 13:00 IST. Cron reads the schedule on the SERVER's clock, so the
# IST time is converted here (07:30 on a UTC server, 13:00 on an IST one).
# Override with CRON_TIME=HH:MM (IST) or CRON_SCHEDULE="..." (server time).
# Re-running replaces the previous entry (matched by its marker comment).

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PYTHON="$PROJECT_DIR/.venv/bin/python"
WRAPPER="$PROJECT_DIR/scripts/run_product_categorisation_sync.sh"
ENV_FILE="$PROJECT_DIR/.env"
CRON_LOG="$PROJECT_DIR/logs/product_categorisation_cron.log"
MARKER="# product-categorisation-sync: 4 sources -> powerbidb.product_categorisation_us_ca_uk_it"
CRON_TIMEZONE="${CRON_TIMEZONE:-Asia/Kolkata}"
CRON_TIME="${CRON_TIME:-13:00}"
RUN_ARGS="${CRON_RUN_ARGS---sync}"

fail() { echo "ERROR: $*" >&2; exit 1; }
ok() { echo "  ok  $*"; }

offset_minutes() {   # UTC offset of a timezone in minutes ("" = server's own)
    local z
    if [[ -n "$1" ]]; then z="$(TZ="$1" date +%z)"; else z="$(date +%z)"; fi
    local total=$(( 10#${z:1:2} * 60 + 10#${z:3:2} ))
    [[ "${z:0:1}" == "-" ]] && total=$(( -total ))
    echo "$total"
}

[[ "$CRON_TIME" =~ ^([01]?[0-9]|2[0-3]):([0-5][0-9])$ ]] || fail "CRON_TIME must be HH:MM, got $CRON_TIME"
IST_MIN=$(( 10#${BASH_REMATCH[1]} * 60 + 10#${BASH_REMATCH[2]} ))
SERVER_MIN=$(( ((IST_MIN - ($(offset_minutes "$CRON_TIMEZONE") - $(offset_minutes ""))) % 1440 + 1440) % 1440 ))
SCHEDULE="${CRON_SCHEDULE-$(( SERVER_MIN % 60 )) $(( SERVER_MIN / 60 )) * * *}"

echo "Verifying deployment in $PROJECT_DIR"
[[ -x "$PYTHON" ]] || fail "virtualenv interpreter missing: $PYTHON (create it: uv sync --no-dev)"
"$PYTHON" -c "import mysql.connector, dotenv" || fail "dependencies not installed in $PYTHON"
ok "interpreter $PYTHON"
[[ -x "$WRAPPER" ]] || fail "wrapper not executable: chmod +x $WRAPPER"
[[ -f "$ENV_FILE" ]] || fail "missing $ENV_FILE (cp .env.example .env && chmod 600 .env)"
[[ "$(stat -c %a "$ENV_FILE")" == "600" ]] || fail "$ENV_FILE must be mode 600: chmod 600 $ENV_FILE"
command -v flock >/dev/null || fail "flock not found (install util-linux)"
"$PYTHON" "$PROJECT_DIR/run.py" --env-file "$ENV_FILE" --check-config --no-lock >/dev/null \
    || fail "configuration check failed; run: $PYTHON $PROJECT_DIR/run.py --check-config"
ok "configuration"
[[ -z "${CRON_SCHEDULE+x}" ]] && ok "schedule: daily $CRON_TIME $CRON_TIMEZONE = '$SCHEDULE' on this server ($(date +%Z))"

ENTRY="$SCHEDULE $WRAPPER${RUN_ARGS:+ $RUN_ARGS} >> $CRON_LOG 2>&1"
# Change check: syncs only when a source table (or the target) changed. 0 = off.
CHECK_EVERY_MINUTES="${CHECK_EVERY_MINUTES:-30}"
CHANGE_MARKER="# product-categorisation-sync (on change): every ${CHECK_EVERY_MINUTES} min"
CHANGE_ENTRY="*/$CHECK_EVERY_MINUTES * * * * $WRAPPER --sync --if-changed >> $PROJECT_DIR/logs/product_categorisation_change_check.log 2>&1"
echo; echo "Cron entries:"; echo "  $MARKER"; echo "  $ENTRY"
[[ "$CHECK_EVERY_MINUTES" != "0" ]] && { echo "  $CHANGE_MARKER"; echo "  $CHANGE_ENTRY"; }
[[ "${1:-}" == "--print" ]] && exit 0

current="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$current" | awk '/^# product-categorisation-sync/ { skip = 1; next } skip == 1 { skip = 0; next } { print }')"
{
    printf '%s\n%s\n%s\n' "$filtered" "$MARKER" "$ENTRY"
    if [[ "$CHECK_EVERY_MINUTES" != "0" ]]; then printf '%s\n%s\n' "$CHANGE_MARKER" "$CHANGE_ENTRY"; fi
} | sed '/^$/N;/^\n$/D' | crontab -
echo; echo "Installed for user $(id -un). Inspect with: crontab -l"
