#!/bin/sh
# Watch production and tell the maintainer when it fails (#303).
#
#   sh host/watch.sh
#
# Each run collects the new `web` and `nginx` log lines into rotated files on
# the host, runs host/check.sh, tests the backup's last-success, counts app
# errors in the new `web` lines, and pings two Healthchecks.io checks with the
# result. A LaunchAgent runs it every five minutes; the deploy skill runs it
# before every pull. Run it from the checkout, like check.sh. The runbook is
# docs/monitoring.md.
#
# Exit 1 means the watcher could not do its own job (settings missing, logs
# unreadable, a ping not delivered). A failing host is reported by the ping,
# not by the exit status.
#
# POSIX sh on purpose: macOS's /bin/sh on the host, dash in CI.

# No -e: one failed step is recorded and the run goes on to the next one.
set -u

# launchd's PATH has neither Docker Desktop's CLI nor Homebrew. Appended, not
# prepended, so a stub earlier on PATH (the test suite's) still wins.
PATH="$PATH:/usr/local/bin:/opt/homebrew/bin"
export PATH
LC_ALL=C
export LC_ALL

here=$(dirname "$0")

# Fail closed: without both ping URLs no run can report anything, and a run
# that pings only one check would look healthy on the other.
WATCH_ENV=${WATCH_ENV:-host/watch.env}
if [ ! -f "$WATCH_ENV" ]; then
    echo "watch: $WATCH_ENV missing, see docs/monitoring.md" >&2
    exit 1
fi
# shellcheck disable=SC1090
. "$WATCH_ENV"
for setting in WATCH_PING_URL WATCH_ERRORS_PING_URL; do
    eval "value=\${$setting:-}"
    if [ -z "$value" ]; then
        echo "watch: $setting not set in $WATCH_ENV, see docs/monitoring.md" >&2
        exit 1
    fi
done
WATCH_LOG_DIR=${WATCH_LOG_DIR:-$HOME/Library/Logs/planning-hub}

# The same settings file backup.sh reads, so the two never disagree on
# where last-success lives. A BACKUP_DIR given on the command line still
# wins, for the alert drill in docs/monitoring.md.
given_backup_dir=${BACKUP_DIR:-}
BACKUP_ENV=${BACKUP_ENV:-backup/backup.env}
if [ -f "$BACKUP_ENV" ]; then
    # shellcheck disable=SC1090
    . "$BACKUP_ENV"
fi
BACKUP_DIR=${given_backup_dir:-${BACKUP_DIR:-$HOME/Backups/planning-hub}}

# Docker's `local` driver uses the same bounds (docker-compose.yml).
ROTATE_BYTES=10485760
ROTATE_KEEP=5
# docs/backup.md: last-success older than about 26 hours is a missed night.
BACKUP_MAX_MINUTES=1560

mkdir -p "$WATCH_LOG_DIR"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
trap 'exit 1' HUP INT TERM

status=0

rotate() {
    [ -f "$1" ] || return 0
    [ "$(wc -c <"$1")" -ge "$ROTATE_BYTES" ] || return 0
    n=$ROTATE_KEEP
    while [ "$n" -gt 1 ]; do
        prev=$((n - 1))
        if [ -f "$1.$prev" ]; then
            mv -f "$1.$prev" "$1.$n"
        fi
        n=$prev
    done
    mv -f "$1" "$1.1"
}

# The LaunchAgent writes this run's output to watch.log, so rotating it here
# sends the next run's output to a fresh file.
rotate "$WATCH_LOG_DIR/watch.log"

printf '%s run\n' "$(date '+%Y-%m-%dT%H:%M:%S')"

# Docker prints a fixed-width UTC timestamp first on every line, so "newer"
# is a plain string comparison. --since takes whole seconds only: the lines
# from the cursor's own second come back and are dropped here.
collect() {
    service=$1
    cursor_file="$WATCH_LOG_DIR/.$service.cursor"
    cursor=$(cat "$cursor_file" 2>/dev/null)
    if [ -n "$cursor" ]; then
        since=${cursor%%.*}
        set -- --since "${since%Z}Z"
    else
        set --
    fi
    if ! docker compose logs --no-color --no-log-prefix --timestamps "$@" "$service" \
        >"$work/$service.all" 2>"$work/$service.err"; then
        printf 'docker compose logs %s failed: %s\n' "$service" "$(head -n 1 "$work/$service.err")"
        rm -f "$work/$service.all"
        return 1
    fi
    awk -v cursor="$cursor" 'cursor == "" || $1 > cursor' "$work/$service.all" >"$work/$service.new"
    [ -s "$work/$service.new" ] || return 0
    rotate "$WATCH_LOG_DIR/$service.log"
    cat "$work/$service.new" >>"$WATCH_LOG_DIR/$service.log"
    awk '$1 > last { last = $1 } END { print last }' "$work/$service.new" >"$cursor_file"
}

collect web || status=1
collect nginx || status=1

# What a ping body may carry: check labels and counts. Never a log line, a
# check's found value or a traceback; Django's error-reporting docs warn
# against sending those over the internet, and the details are in the
# collected logs on this host.
production=
errors=

# A state failure alerts only on the second run in a row: a deploy rebuilds
# web within about a minute, and the host is back about five minutes after a
# power cut. Neither should page anyone.
check_out=$(sh "$here/check.sh" 2>&1)
check_status=$?
printf '%s\n' "$check_out"
marker="$WATCH_LOG_DIR/.check-failed"
if [ "$check_status" -eq 0 ]; then
    rm -f "$marker"
elif [ -f "$marker" ]; then
    labels=$(printf '%s\n' "$check_out" | sed -n 's/^\(FAIL [^:]*\):.*/\1/p')
    production=${labels:-FAIL host check}
else
    : >"$marker"
fi

# 26 hours is already slow, so a stale backup alerts on the first run.
if [ -z "$(find "$BACKUP_DIR/last-success" -mmin "-$BACKUP_MAX_MINUTES" 2>/dev/null)" ]; then
    production=${production:+$production
}"FAIL backup fresh"
fi

count() {
    n=$(grep -c "$1" "$work/web.new" 2>/dev/null)
    echo "${n:-0}"
}

# Errors are events, not states, so they alert on the first run. One Notion
# or Claude failure is something the user sees in the UI; three in five
# minutes are an outage or a broken key. The views answer those failures
# with "Bad Gateway:" lines, which are not counted a second time.
unhandled=$(count ' ERROR django.request Internal Server Error: ')
notion=$(count ' Notion call failed: ')
claude=$(count ' claude_call call=.* outcome=error')
if [ "$unhandled" -ge 1 ]; then
    errors="$unhandled unhandled errors (500)"
fi
if [ "$notion" -ge 3 ]; then
    errors=${errors:+$errors
}"$notion Notion failures"
fi
if [ "$claude" -ge 3 ]; then
    errors=${errors:+$errors
}"$claude Claude failures"
fi

ping() {
    if [ -n "$2" ]; then
        printf 'alert %s: %s\n' "$3" "$(printf '%s' "$2" | tr '\n' ';')"
        curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "$2" "$1/fail"
    else
        curl -fsS -m 10 --retry 3 -o /dev/null "$1"
    fi
}

ping "$WATCH_PING_URL" "$production" production || status=1
ping "$WATCH_ERRORS_PING_URL" "$errors" errors || status=1

exit "$status"
