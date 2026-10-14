#!/bin/sh
# Back up the production database and prove the dump restores (#300).
#
#   backup/backup.sh                 dump, verify, keep, prune
#   backup/backup.sh --local-only    the same; skips the off-site copy (PR 2)
#   backup/backup.sh --verify FILE   restore FILE into a throwaway and compare
#                                    it with the live database; writes nothing
#
# Runs on the production host, from launchd at 03:30 and from the deploy
# skill before every pull. The runbook is docs/backup.md.
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
        # Accepted from PR 1 on: the deploy calls it with the script the host
        # already has, before pulling the one that adds the off-site leg.
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
: "$local_only"

# One "table|md5" line per table, in name order. Every row is rendered as
# text and sorted, so the sum depends on the data, not on physical order.
checksum_sql() {
    printf '%s\n' "SELECT format('SELECT %L, md5(coalesce(string_agg(t::text, E''\\n'' ORDER BY t::text), '''')) FROM %I.%I t', tablename, schemaname, tablename) FROM pg_tables WHERE schemaname = 'public'"
    for table in $SKIP_DATA; do
        printf "  AND tablename <> '%s'\n" "$table"
    done
    printf '%s\n' "ORDER BY tablename \\gexec"
}

# Restore $1 into a throwaway postgres:16 with the runbook's own restore
# command, then compare every table's checksum with the live database.
verify() {
    dump=$1
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

    log "comparing table checksums with the live database"
    docker compose exec -T -e PGTZ=UTC db \
        psql -X -At -v ON_ERROR_STOP=1 -U "$user" -d "$db" \
        < "$workdir/checksums.sql" > "$workdir/live" \
        || fail "cannot checksum the live database"
    docker exec -i -e PGTZ=UTC "$container" \
        psql -X -At -v ON_ERROR_STOP=1 -U "$user" -d "$db" \
        < "$workdir/checksums.sql" > "$workdir/restored" \
        || fail "cannot checksum the restored database"

    [ -s "$workdir/live" ] || fail "no tables to compare in the live database"
    if ! cmp -s "$workdir/live" "$workdir/restored"; then
        for table in $(cut -d'|' -f1 "$workdir/live" "$workdir/restored" | sort -u); do
            a=$(grep "^$table|" "$workdir/live" || true)
            b=$(grep "^$table|" "$workdir/restored" || true)
            if [ "$a" != "$b" ]; then
                printf '%s ERROR checksum mismatch in table %s\n' \
                    "$(date '+%Y-%m-%dT%H:%M:%S')" "$table" >&2
            fi
        done
        fail "the restored data differs from the live database"
    fi
    log "verified $(wc -l < "$workdir/live" | tr -d ' ') tables"

    docker rm -fv "$container" >/dev/null 2>&1 || true
    container=
}

if [ -n "$verify_file" ]; then
    [ -r "$verify_file" ] || fail "cannot read $verify_file"
    verify "$verify_file"
    log "done: $verify_file restores and matches the live database"
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

mv "$partial" "$BACKUP_DIR/$name"
partial=
log "kept $BACKUP_DIR/$name ($(wc -c < "$BACKUP_DIR/$name" | tr -d ' ') bytes)"

# Retention by count and by name, after success only: if the job fails for
# weeks, pruning by age would delete every good backup.
count=0
for f in "$BACKUP_DIR"/planning_hub-*.dump; do
    [ -e "$f" ] && count=$((count + 1))
done
excess=$((count - BACKUP_KEEP))
for f in "$BACKUP_DIR"/planning_hub-*.dump; do
    [ "$excess" -gt 0 ] || break
    rm -f "$f"
    log "pruned $(basename "$f")"
    excess=$((excess - 1))
done

# The contract with #303: written only when every step succeeded.
partial="$BACKUP_DIR/last-success.partial"
printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$name" > "$partial"
mv "$partial" "$BACKUP_DIR/last-success"
partial=
log "done"
