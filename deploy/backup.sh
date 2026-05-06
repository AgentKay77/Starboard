#!/bin/bash
# Daily SQLite backup. Crontab line:
#   5 2 * * *  /opt/starboard/deploy/backup.sh
set -euo pipefail

DB_PATH="${DATABASE_PATH:-/opt/starboard/starboard.db}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/starboard}"
RETAIN_DAYS="${RETAIN_DAYS:-30}"

mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)
TARGET="$BACKUP_DIR/starboard-$STAMP.db"

# Use the SQLite .backup command — atomic with WAL.
sqlite3 "$DB_PATH" ".backup $TARGET"

# Prune older than RETAIN_DAYS.
find "$BACKUP_DIR" -name 'starboard-*.db' -mtime +"$RETAIN_DAYS" -delete

echo "Backed up to $TARGET"
