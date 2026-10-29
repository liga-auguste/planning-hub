#!/bin/sh
# Check the production host's macOS settings and running state (#309).
#
#   sh host/check.sh
#
# One line per check, `ok   <label>` or `FAIL <label>: <found>. Fix: <how>`,
# and exit 1 if any check failed. Read-only and without sudo: a failure
# prints its fix instead of applying it. Run it from the checkout, so
# `docker compose` finds docker-compose.yml. What each setting is for is in
# docs/production-host.md.
#
# POSIX sh on purpose: macOS's /bin/sh on the host, dash in CI.

# No -e: a failed check is recorded and the run goes on to the next one.
set -u

# A plain SSH session has neither Docker Desktop's CLI nor Homebrew on PATH.
# Appended, not prepended, so a stub earlier on PATH (the test suite's) still
# wins.
PATH="$PATH:/usr/local/bin:/opt/homebrew/bin"
export PATH
LC_ALL=C
export LC_ALL

failed=0

ok() {
    printf 'ok   %s\n' "$1"
}

fail() {
    printf 'FAIL %s: %s. Fix: %s\n' "$1" "$2" "$3"
    failed=1
}

# Fail closed: a value that cannot be read counts as wrong, never as right.
pmset_out=$(pmset -g 2>/dev/null)

# The line, not just the value: `pmset -g` annotates it with whatever holds
# the setting off, e.g. `sleep 1 (sleep prevented by caffeinate, powerd)`.
pmset_line() {
    printf '%s\n' "$pmset_out" | awk -v key="$1" '$1 == key { $1 = $1; print; exit }'
}

label="restart after power loss"
line=$(pmset_line autorestart)
if [ "$line" = "autorestart 1" ]; then
    ok "$label"
else
    fail "$label" "${line:-no autorestart line in pmset -g}" "sudo pmset -a autorestart 1"
fi

# `sleep` is the system sleep timer, a stored setting, and only its value
# counts. pmset annotates the line whenever an assertion holds sleep off,
# whatever the value, so `sleep 0 (sleep prevented by …)` passes, while
# `sleep 1 (… caffeinate)` fails: caffeinate lasts only as long as its process.
label="no idle sleep"
line=$(pmset_line sleep)
case $line in
    "sleep 0" | "sleep 0 "*) ok "$label" ;;
    *) fail "$label" "${line:-no sleep line in pmset -g}" "sudo pmset -a sleep 0" ;;
esac

# Not `systemsetup -gettimezone`: without admin rights it prints a refusal
# and still exits 0.
label="time zone Europe/Berlin"
zone=$(readlink /etc/localtime 2>/dev/null)
case $zone in
    */zoneinfo/Europe/Berlin) ok "$label" ;;
    *) fail "$label" "${zone:-/etc/localtime unreadable}" \
        "sudo systemsetup -settimezone Europe/Berlin, then restart" ;;
esac

label="FileVault off"
status=$(fdesetup status 2>/dev/null | head -n 1)
if [ "$status" = "FileVault is Off." ]; then
    ok "$label"
else
    fail "$label" "${status:-fdesetup status unreadable}" \
        "docs/production-host.md, FileVault"
fi

label="automatic login"
user=$(id -un)
login=$(defaults read /Library/Preferences/com.apple.loginwindow autoLoginUser 2>/dev/null)
if [ -n "$login" ] && [ "$login" = "$user" ]; then
    ok "$label"
else
    fail "$label" "${login:-no automatic login}, expected $user" \
        "System Settings > Users & Groups > Automatically log in as"
fi

# Docker documents settings-store.json but not its keys; AutoStart is what
# Docker Desktop 4.80 writes. A renamed key fails here rather than passing.
# `-o -` matters: without it plutil writes the extracted value over the file.
# Over SSH, macOS refuses the read unless Remote Login grants full disk
# access, so an unreadable file says so.
label="Docker Desktop at sign-in"
autostart=$(plutil -extract AutoStart raw -o - \
    "$HOME/Library/Group Containers/group.com.docker/settings-store.json" 2>/dev/null)
if [ "$autostart" = "true" ]; then
    ok "$label"
else
    fail "$label" \
        "AutoStart ${autostart:-unreadable (over SSH: needs full disk access for remote users)}" \
        "Docker Desktop > Settings > General > Start Docker Desktop when you sign in"
fi

# A private Wi-Fi address is random and locally administered: the IEEE 802
# U/L bit, 0x02 in the first octet, is set. A hardware MAC never has it, and
# the router's address reservation belongs to the hardware MAC. The default
# route's interface, so the check stays right on Ethernet.
label="hardware MAC on the network"
fix="System Settings > Wi-Fi > Details > Private Wi-Fi address > Off"
interface=$(route -n get default 2>/dev/null | awk '$1 == "interface:" { print $2; exit }')
if [ -z "$interface" ]; then
    fail "$label" "no default route" "$fix"
else
    mac=$(ifconfig "$interface" 2>/dev/null | awk '$1 == "ether" { print $2; exit }')
    first=${mac%%:*}
    case $first in
        [0-9a-fA-F] | [0-9a-fA-F][0-9a-fA-F])
            if [ $((0x$first & 2)) -eq 0 ]; then
                ok "$label"
            else
                fail "$label" "$interface has the private address $mac" "$fix"
            fi
            ;;
        *) fail "$label" "no MAC on $interface" "$fix" ;;
    esac
fi

label="Docker answers"
if docker info >/dev/null 2>&1; then
    ok "$label"
else
    fail "$label" "docker info failed" "start Docker Desktop"
fi

# The health check is DB-aware, so a healthy web covers db as well.
label="web healthy"
fix="docker compose up -d, then docker compose logs web"
web=$(docker compose ps -q web 2>/dev/null)
if [ -z "$web" ]; then
    fail "$label" "no web container" "$fix"
else
    health=$(docker inspect --format '{{.State.Health.Status}}' "$web" 2>/dev/null)
    if [ "$health" = "healthy" ]; then
        ok "$label"
    else
        fail "$label" "${health:-no health status}" "$fix"
    fi
fi

# 401 is Basic Auth answering: nginx is up and in front of the app.
label="nginx answers 401"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://localhost/ 2>/dev/null)
if [ "$code" = "401" ]; then
    ok "$label"
else
    fail "$label" "HTTP ${code:-no answer}" \
        "docker compose up -d, then docker compose logs nginx"
fi

label="backup agent loaded"
if launchctl print "gui/$(id -u)/de.ligaauguste.planninghub.backup" >/dev/null 2>&1; then
    ok "$label"
else
    fail "$label" "launchctl print failed" "docs/backup.md, Install"
fi

exit "$failed"
