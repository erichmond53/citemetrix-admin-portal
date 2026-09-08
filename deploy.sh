#!/bin/bash

# CiteMetrix Admin Portal deploy — source to production with rollback

set -uo pipefail



SOURCE_DIR="/home/ubuntu/admin-portal"

PROD_DIR="/var/www/admin-portal"

BACKUP_DIR="/var/backups/admin-portal"

LOG_FILE="$SOURCE_DIR/deploy.log"

SERVICE="adminportal"

EXPECTED_HOSTNAME="ip-172-26-9-37"

TIMESTAMP=$(date -u +%Y%m%d-%H%M%S)



log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $1" | tee -a "$LOG_FILE"; }

fail() { log "FAIL: $1"; exit 1; }



# Common rsync excludes used by both backup and sync steps

RSYNC_EXCLUDES="--exclude=venv/ --exclude=.env --exclude=*.sock --exclude=__pycache__/ --exclude=*.pyc --exclude=deploy.sh --exclude=deploy.log --exclude=.gitignore --exclude=.git/ --exclude=reports/"



# Step 1: Sanity check

if [[ "$(hostname)" != "$EXPECTED_HOSTNAME" ]]; then fail "Hostname mismatch. Got $(hostname), expected $EXPECTED_HOSTNAME."; fi

if [[ ! -d "$SOURCE_DIR" ]]; then fail "Source dir $SOURCE_DIR missing."; fi

if [[ ! -d "$PROD_DIR" ]]; then fail "Prod dir $PROD_DIR missing."; fi



log "Deploy started."



# Step 2: Lint

log "Linting Python files..."

cd "$SOURCE_DIR" || fail "cd to $SOURCE_DIR failed"

for pyfile in *.py; do

    if [[ -f "$pyfile" ]]; then

        python3 -m py_compile "$pyfile" 2>&1 | tee -a "$LOG_FILE"

        if [[ ${PIPESTATUS[0]} -ne 0 ]]; then fail "Syntax error in $pyfile."; fi

    fi

done

log "Lint passed."



# Step 3: Backup production

BACKUP_PATH="$BACKUP_DIR/prod-backup-${TIMESTAMP}"

log "Backing up production to $BACKUP_PATH..."

mkdir -p "$BACKUP_PATH"

rsync -a $RSYNC_EXCLUDES "$PROD_DIR/" "$BACKUP_PATH/" 2>&1 | tee -a "$LOG_FILE"

if [[ ${PIPESTATUS[0]} -ne 0 ]]; then fail "Backup rsync failed."; fi



# Retention: keep 10 most recent

cd "$BACKUP_DIR" && ls -1dt prod-backup-* 2>/dev/null | tail -n +11 | xargs -r rm -rf

log "Backup complete."



# Step 4: Sync source to production

log "Syncing source to production..."

rsync -a --delete $RSYNC_EXCLUDES "$SOURCE_DIR/" "$PROD_DIR/" 2>&1 | tee -a "$LOG_FILE"

if [[ ${PIPESTATUS[0]} -ne 0 ]]; then

    log "Sync rsync failed. Attempting rollback..."

    rsync -a --delete $RSYNC_EXCLUDES "$BACKUP_PATH/" "$PROD_DIR/"

    fail "Sync failed and rollback attempted."

fi

log "Sync complete."



# Step 5: Restart service

log "Restarting $SERVICE..."

sudo systemctl restart "$SERVICE"

RESTART_STATUS=$?



# Step 6: Verify

sleep 2

ROLLBACK_NEEDED=0

if [[ $RESTART_STATUS -ne 0 ]]; then log "ERROR: systemctl restart failed."; ROLLBACK_NEEDED=1; fi

if ! sudo systemctl is-active --quiet "$SERVICE"; then log "ERROR: $SERVICE not active."; ROLLBACK_NEEDED=1; fi



HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -H "Host: admin.citemetrix.com" http://localhost/ 2>/dev/null || echo "000")

if [[ "$HTTP_STATUS" == "000" || "$HTTP_STATUS" == "502" || "$HTTP_STATUS" == "503" || "$HTTP_STATUS" == "504" ]]; then

    log "ERROR: HTTP check failed with status $HTTP_STATUS"

    ROLLBACK_NEEDED=1

else

    log "HTTP check OK: status $HTTP_STATUS"

fi



# Step 7: Rollback on failure

if [[ "$ROLLBACK_NEEDED" == "1" ]]; then

    log "ROLLBACK: restoring from $BACKUP_PATH..."

    rsync -a --delete $RSYNC_EXCLUDES "$BACKUP_PATH/" "$PROD_DIR/"

    sudo systemctl restart "$SERVICE"

    sleep 2

    if sudo systemctl is-active --quiet "$SERVICE"; then log "Rollback complete."; else log "CRITICAL: rollback ALSO failed."; fi

    exit 1

fi



log "Deploy SUCCESS."

exit 0

