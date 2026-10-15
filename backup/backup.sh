#!/bin/sh
# Back up the production database and prove the dump restores (#300).
#
#   backup/backup.sh                 dump, verify, keep, send an encrypted
#                                    copy off-site, prune, record success
#   backup/backup.sh --local-only    dump, verify, keep, prune; no off-site
#                                    copy, and last-success stays as it is
#   backup/backup.sh --verify FILE   restore FILE into a throwaway and compare
#                                    it with FILE.sums, the checksums recorded
#                                    when it was taken; writes nothing
#
# Runs on the production host, from launchd at 03:30 and from the deploy
# skill before every pull (--local-only). The off-site copy goes to the VPS,
# where backup/receive.sh takes it. The runbook is docs/backup.md.
#
# POSIX sh on purpose: macOS's /bin/sh in production, dash in CI. No
# `pipefail` exists there, so no pipeline below carries an exit status that
# matters.

set -eu

# launchd's PATH has neither Docker Desktop's CLI nor Homebrew. Appended, not
# prepended, so a stub earlier on PATH (the test suite's) still wins.
PATH="$PATH:/usr/local/bin:/opt/homebrew/bin"
export PATH
LC_ALL=C
export LC_ALL

# The repository root, from this file's own location, so the LaunchAgent
# needs only the one path and `docker compose` finds docker-compose.yml.
cd "$(dirname "$0")/.."

BACKUP_ENV=${BACKUP_ENV:-backup/backup.env}
if [ -f "$BACKUP_ENV" ]; then
    # shellcheck disable=SC1090
    . "$BACKUP_ENV"
fi
BACKUP_DIR=${BACKUP_DIR:-$HOME/Backups/planning-hub}
BACKUP_KEEP=${BACKUP_KEEP:-90}

# Rows that can be rebuilt. Their tables are still dumped and restored.
SKIP_DATA="django_session planning_hub_cache"

log() {
    printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"
}

fail() {
    printf '%s ERROR %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >&2
    exit 1
}

container=
partial=
sums=
workdir=

cleanup() {
    # -v: postgres:16 declares a VOLUME for its data directory, so every
    # throwaway gets an anonymous volume that `rm -f` alone leaves behind.
    if [ -n "$container" ]; then
        docker rm -fv "$container" >/dev/null 2>&1 || true
    fi
    if [ -n "$partial" ]; then
        rm -f "$partial" "$BACKUP_DIR/last-success.partial"
    fi
    if [ -n "$sums" ]; then
        rm -f "$sums"
    fi
    if [ -n "$workdir" ]; then
        rm -rf "$workdir"
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

local_only=
verify_file=
while [ $# -gt 0 ]; do
    case $1 in
        # Accepted from the first version on: the deploy calls it with the
        # script the host already has, before the pull.
        --local-only) local_only=1 ;;
        --verify)
            [ $# -ge 2 ] || fail "--verify needs a file"
            verify_file=$2
            shift
            ;;
        *) fail "unknown option: $1" ;;
    esac
    shift
done
# One "table|md5" line per table, in name order. Every row is rendered as
# text and sorted, so the sum depends on the data, not on physical order.
# Sorted by byte order (COLLATE "C"), not by the database's en_US.utf8: that
# order comes from the libc in the postgres:16 image, so a re-pulled image
# could sort differently and fail --verify on an intact dump. Changing the
# sort later would invalidate every .sums already written.
checksum_sql() {
    printf '%s\n' "SELECT format('SELECT %L, md5(coalesce(string_agg(t::text, E''\\n'' ORDER BY t::text COLLATE \"C\"), '''')) FROM %I.%I t', tablename, schemaname, tablename) FROM pg_tables WHERE schemaname = 'public'"
    for table in $SKIP_DATA; do
        printf "  AND tablename <> '%s'\n" "$table"
    done
    printf '%s\n' "ORDER BY tablename \\gexec"
}

# Restore $1 into a throwaway postgres:16 with the runbook's own restore
# command, then compare every table's checksum with $2, the expected sums.
# Without $2 the expectation is the live database, checksummed after the
# restore: right for a dump taken seconds ago, wrong for any older one, since
# every later write would read as a mismatch.
verify() {
    dump=$1
    expected=${2:-}
    user=$(docker compose exec -T db printenv POSTGRES_USER) \
        || fail "cannot read POSTGRES_USER from the db service; is the stack running?"
    db=$(docker compose exec -T db printenv POSTGRES_DB) \
        || fail "cannot read POSTGRES_DB from the db service"

    workdir=$(mktemp -d)
    checksum_sql > "$workdir/checksums.sql"

    # No network, so trust auth reaches nobody; the live user, so ownership
    # restores the way it would in production.
    container="planninghub-backup-verify-$$"
    log "starting throwaway postgres:16 ($container)"
    docker run -d --name "$container" --network none \
        -e POSTGRES_USER="$user" -e POSTGRES_HOST_AUTH_METHOD=trust \
        postgres:16 >/dev/null \
        || fail "cannot start the throwaway postgres:16 container"

    # Over TCP: during init the image runs a server on the Unix socket only.
    tries=0
    until docker exec "$container" pg_isready -q -h 127.0.0.1 -U "$user" >/dev/null 2>&1; do
        tries=$((tries + 1))
        [ "$tries" -lt 60 ] || fail "the throwaway postgres did not become ready in 60s"
        sleep 1
    done

    log "restoring $dump into the throwaway"
    docker exec -i "$container" \
        pg_restore --clean --if-exists --create --exit-on-error -U "$user" -d postgres \
        < "$dump" \
        || fail "the dump does not restore: $dump"

    if [ -n "$expected" ]; then
        log "comparing table checksums with $expected"
        against="the checksums recorded when the dump was taken"
    else
        log "comparing table checksums with the live database"
        expected="$workdir/live"
        against="the live database"
        docker compose exec -T -e PGTZ=UTC db \
            psql -X -At -v ON_ERROR_STOP=1 -U "$user" -d "$db" \
            < "$workdir/checksums.sql" > "$expected" \
            || fail "cannot checksum the live database"
    fi
    docker exec -i -e PGTZ=UTC "$container" \
        psql -X -At -v ON_ERROR_STOP=1 -U "$user" -d "$db" \
        < "$workdir/checksums.sql" > "$workdir/restored" \
        || fail "cannot checksum the restored database"

    [ -s "$expected" ] || fail "no tables to compare in $against"
    if ! cmp -s "$expected" "$workdir/restored"; then
        for table in $(cut -d'|' -f1 "$expected" "$workdir/restored" | sort -u); do
            a=$(grep "^$table|" "$expected" || true)
            b=$(grep "^$table|" "$workdir/restored" || true)
            if [ "$a" != "$b" ]; then
                printf '%s ERROR checksum mismatch in table %s\n' \
                    "$(date '+%Y-%m-%dT%H:%M:%S')" "$table" >&2
            fi
        done
        fail "the restored data differs from $against"
    fi
    log "verified $(wc -l < "$expected" | tr -d ' ') tables"

    docker rm -fv "$container" >/dev/null 2>&1 || true
    container=
}

# Encrypt $1 for the age recipient and send it to the receiver on the VPS.
# The host holds only the recipient (the public key), so neither it nor the
# VPS can read the copy. Written to a file, checked, then sent: no pipeline,
# since without `pipefail` a failed `age` would go unnoticed.
offsite() {
    # Fail closed: a run that silently skipped the off-site copy would look
    # like success, and #303 would never see it.
    [ -n "${BACKUP_AGE_RECIPIENT:-}" ] \
        || fail "BACKUP_AGE_RECIPIENT is not set; the off-site copy needs it (or use --local-only)"
    [ -n "${BACKUP_REMOTE:-}" ] \
        || fail "BACKUP_REMOTE is not set; the off-site copy needs it (or use --local-only)"
    [ -n "${BACKUP_SSH_KEY:-}" ] \
        || fail "BACKUP_SSH_KEY is not set; the off-site copy needs it (or use --local-only)"
    [ -r "$BACKUP_SSH_KEY" ] || fail "cannot read BACKUP_SSH_KEY: $BACKUP_SSH_KEY"

    encrypted="$workdir/$(basename "$1").age"
    log "encrypting $(basename "$1") for the off-site copy"
    age -r "$BACKUP_AGE_RECIPIENT" -o "$encrypted" "$1" || fail "age failed"
    [ -s "$encrypted" ] || fail "age wrote nothing"

    # BatchMode: no prompt, and an unknown host key is refused rather than
    # learned. The receiver names the file and answers with that name.
    log "sending the encrypted copy to $BACKUP_REMOTE"
    receipt=$(ssh -i "$BACKUP_SSH_KEY" -o BatchMode=yes -o IdentitiesOnly=yes \
        "$BACKUP_REMOTE" < "$encrypted") \
        || fail "the upload to $BACKUP_REMOTE failed"
    log "off-site: $receipt"
}

if [ -n "$verify_file" ]; then
    [ -r "$verify_file" ] || fail "cannot read $verify_file"
    [ -r "$verify_file.sums" ] \
        || fail "no checksums for $verify_file: $verify_file.sums is missing"
    verify "$verify_file" "$verify_file.sums"
    log "done: $verify_file restores to the data it held when it was taken"
    exit 0
fi

mkdir -p "$BACKUP_DIR"
name="planning_hub-$(date '+%Y-%m-%d_%H%M%S').dump"
partial="$BACKUP_DIR/$name.partial"

log "dumping the database to $name"
exclude=
for table in $SKIP_DATA; do
    exclude="$exclude --exclude-table-data=$table"
done
# -T: compose allocates a TTY by default, and a TTY corrupts a binary stream.
docker compose exec -T db \
    sh -c "pg_dump -Fc -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\"$exclude" \
    > "$partial" \
    || fail "pg_dump failed"

verify "$partial"

# The restored side's sums, which equal live at this moment. They let
# --verify check this dump later, however much the live data has moved on.
sums="$BACKUP_DIR/$name.sums"
cp "$workdir/restored" "$sums"
mv "$partial" "$BACKUP_DIR/$name"
partial=
sums=
log "kept $BACKUP_DIR/$name ($(wc -c < "$BACKUP_DIR/$name" | tr -d ' ') bytes)"

# A failed upload leaves the dump above in place: the local tier is good.
if [ -n "$local_only" ]; then
    log "skipping the off-site copy (--local-only)"
else
    offsite "$BACKUP_DIR/$name"
fi

# Retention by count and by name, after success only: if the job fails for
# weeks, pruning by age would delete every good backup.
count=0
for f in "$BACKUP_DIR"/planning_hub-*.dump; do
    [ -e "$f" ] && count=$((count + 1))
done
excess=$((count - BACKUP_KEEP))
for f in "$BACKUP_DIR"/planning_hub-*.dump; do
    [ "$excess" -gt 0 ] || break
    rm -f "$f" "$f.sums"
    log "pruned $(basename "$f")"
    excess=$((excess - 1))
done

# The contract with #303: written only when every step succeeded, the
# off-site copy included. A --local-only run did not make one, so it leaves
# the file alone rather than hide a failing nightly off-site leg.
if [ -n "$local_only" ]; then
    log "done (local only; last-success unchanged)"
    exit 0
fi
partial="$BACKUP_DIR/last-success.partial"
printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$name" > "$partial"
mv "$partial" "$BACKUP_DIR/last-success"
partial=
log "done"
