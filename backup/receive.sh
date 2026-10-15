#!/bin/sh
# Receive one encrypted backup on the off-site host (#300).
#
# Installed root-owned as /usr/local/bin/planninghub-receive and run only as
# the forced command of the backup user's one key in authorized_keys:
#
#   restrict,command="/usr/local/bin/planninghub-receive" ssh-ed25519 AAAA…
#
# It writes stdin to a file it names from its own clock, rejects an empty or
# oversized upload, never overwrites, and prunes what is both beyond the
# newest RECEIVE_KEEP and older than RECEIVE_KEEP_DAYS. What the client asked
# to run (SSH_ORIGINAL_COMMAND) is never read, so the sending host cannot
# choose names, overwrite, list or read anything.
#
# Nor can it push out recent copies. Its key sits on the production host
# without a passphrase, so whoever takes that host holds it too, and the
# off-site copy exists to survive exactly that. Pruned by count alone, 90
# one-byte uploads (one per second, about 90 s) would evict every real copy.
# So a copy younger than RECEIVE_KEEP_DAYS is never pruned, whatever arrives
# after it, and a flood runs into MAX_FILES or MAX_STORE_BYTES instead: the
# upload is refused, the nightly run fails, last-success goes stale, and the
# history is still there.
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
RECEIVE_KEEP_DAYS=90
# It shares a disk with the public demo. A dump is tens of KB.
MAX_BYTES=52428800
# A full store refuses uploads rather than prune: about 90 copies are normal,
# so 400 leaves room for drills and reruns, and 1 GiB for a far larger
# database. Neither is reached by nightly runs alone.
MAX_FILES=400
MAX_STORE_BYTES=1073741824

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

stored=0
stored_bytes=0
for f in "$RECEIVE_DIR"/planning_hub-*.dump.age; do
    [ -e "$f" ] || continue
    stored=$((stored + 1))
    stored_bytes=$((stored_bytes + $(wc -c < "$f" | tr -d ' ')))
done
[ "$stored" -lt "$MAX_FILES" ] \
    || fail "the store holds $stored copies already (limit $MAX_FILES); nothing kept"

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
[ $((stored_bytes + size)) -le "$MAX_STORE_BYTES" ] \
    || fail "the store would exceed $MAX_STORE_BYTES bytes; nothing kept"

# ln, not mv: it refuses an existing target, so nothing is ever overwritten.
ln "$partial" "$final" 2>/dev/null || fail "$name exists already; not overwriting it"
rm -f "$partial"
partial=
printf 'stored %s (%s bytes)\n' "$name" "$size"

# Retention, after a stored upload only: oldest first by name, and only what
# is beyond the newest RECEIVE_KEEP *and* older than RECEIVE_KEEP_DAYS. Age is
# the file's mtime, set by this host's clock when it was stored, so the
# sender cannot backdate it. The first copy too young to go ends the loop:
# everything after it by name is younger still.
excess=$((stored + 1 - RECEIVE_KEEP))
for f in "$RECEIVE_DIR"/planning_hub-*.dump.age; do
    [ "$excess" -gt 0 ] || break
    [ -n "$(find "$f" -prune -mtime +"$RECEIVE_KEEP_DAYS")" ] || break
    rm -f "$f"
    excess=$((excess - 1))
done
