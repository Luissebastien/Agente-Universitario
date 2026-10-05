#!/bin/sh
# Consistent backup of the Agente U database, plus a disk-space warning.
#
# Uses sqlite3's own .backup, not cp: the scheduler may be mid-write, and a
# plain copy of a live SQLite file can be torn. .backup takes a read lock and
# produces a valid database.
#
# The downloaded originals are NOT backed up: they are content-addressed
# copies of files Moodle still holds, and re-ingesting them costs one sync.
# The database is the only thing that cannot be rebuilt.
#
# Installed as agente-u-backup.service, run daily by agente-u-backup.timer.

set -eu

DB="${AGENTE_U_DB:-/var/lib/agente-u/agente_u.sqlite3}"
DEST="${AGENTE_U_BACKUP_DIR:-/var/backups/agente-u}"
KEEP="${AGENTE_U_BACKUP_KEEP:-14}"
MIN_FREE_MB="${AGENTE_U_MIN_FREE_MB:-1024}"

if [ ! -f "$DB" ]; then
    echo "No database at $DB yet; nothing to back up."
    exit 0
fi

mkdir -p "$DEST"
chmod 700 "$DEST"

# Warn loudly before the disk fills: the scheduler degrades per item when it
# runs out of space, but a full disk is an operator problem, not a data one.
free_mb=$(df -Pm "$DEST" | awk 'NR==2 {print $4}')
if [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
    echo "WARNING: only ${free_mb} MB free on $(df -P "$DEST" | awk 'NR==2 {print $6}')," \
         "below the ${MIN_FREE_MB} MB threshold." >&2
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="$DEST/agente_u-$stamp.sqlite3"

sqlite3 "$DB" ".backup '$target'"
chmod 600 "$target"

# Fail the run if the copy is not a usable database.
if ! sqlite3 "$target" "PRAGMA integrity_check;" | grep -qx "ok"; then
    echo "ERROR: integrity_check failed for $target" >&2
    exit 1
fi

gzip -9 "$target"
echo "Backup written: $target.gz ($(du -h "$target.gz" | cut -f1)), ${free_mb} MB free"

# Keep the newest $KEEP backups.
ls -1t "$DEST"/agente_u-*.sqlite3.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
    echo "Removing old backup: $old"
    rm -f "$old"
done
