#!/bin/sh
# Receive one encrypted backup on the off-site host (#300).
#
# Installed root-owned as /usr/local/bin/planninghub-receive and run only as
# the forced command of the backup user's one key in authorized_keys:
#
#   restrict,command="/usr/local/bin/planninghub-receive" ssh-ed25519 AAAA…
#
# It writes stdin to a file it names from its own clock, rejects an empty or
# oversized upload, never overwrites, and prunes to the newest RECEIVE_KEEP.
# What the client asked to run (SSH_ORIGINAL_COMMAND) is never read, so the
# sending host cannot choose names, overwrite, list, read or delete anything.
#
# This file is a copy on the host: a change here needs a reinstall
# (docs/backup.md, "Install"); a deploy does not do it.
#
# POSIX sh on purpose: dash on the VPS, and in CI.

set -eu
umask 077
LC_ALL=C
export LC_ALL

RECEIVE_DIR=${RECEIVE_DIR:-$HOME/planning-hub}
RECEIVE_KEEP=${RECEIVE_KEEP:-90}
# It shares a disk with the public demo. A dump is tens of KB.
MAX_BYTES=52428800

fail() {
    printf 'ERROR %s\n' "$*" >&2
    exit 1
}

partial=
cleanup() {
    if [ -n "$partial" ]; then
        rm -f "$partial"
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

mkdir -p "$RECEIVE_DIR"
name="planning_hub-$(date -u '+%Y-%m-%d_%H%M%S').dump.age"
final="$RECEIVE_DIR/$name"
[ ! -e "$final" ] || fail "$name exists already; not overwriting it"

# noclobber makes creating the .partial atomic, so two uploads in the same
# second cannot write into one file.
tmp="$final.partial"
(set -C && : > "$tmp") 2>/dev/null || fail "$name is being received already"
partial=$tmp

# One byte past the cap is enough to know it is over; the rest is not read.
head -c "$((MAX_BYTES + 1))" > "$partial"
size=$(wc -c < "$partial" | tr -d ' ')
# An aborted connection must not leave a zero-byte "backup" that pushes a
# real one out of retention.
[ "$size" -gt 0 ] || fail "empty upload; nothing kept"
[ "$size" -le "$MAX_BYTES" ] || fail "upload over $MAX_BYTES bytes; nothing kept"

# ln, not mv: it refuses an existing target, so nothing is ever overwritten.
ln "$partial" "$final" 2>/dev/null || fail "$name exists already; not overwriting it"
rm -f "$partial"
partial=
printf 'stored %s (%s bytes)\n' "$name" "$size"

# Retention by count and by name, after a stored upload only.
count=0
for f in "$RECEIVE_DIR"/planning_hub-*.dump.age; do
    [ -e "$f" ] && count=$((count + 1))
done
excess=$((count - RECEIVE_KEEP))
for f in "$RECEIVE_DIR"/planning_hub-*.dump.age; do
    [ "$excess" -gt 0 ] || break
    rm -f "$f"
    excess=$((excess - 1))
done
