# Production host settings

Implements [Issue #309](https://github.com/liga-auguste/planning-hub/issues/309).

Production runs on a Mac mini, and it depends on macOS settings that exist only on that
machine. Two of them were found wrong in October 2026: the host did not restart after a
power failure, and it ran on US Pacific time, so the 03:30 backup would have fired at
12:30. A replacement machine, or a reset of this one, loses all of them without anyone
noticing, and CI cannot see any of them. This page lists them, and `host/check.sh` checks
them in one run.

Every command below runs **on the production host, in the checkout**, unless it says
otherwise.

## The chain after a power cut

Production comes back on its own only if every link holds. The numbers are rows in the
[settings table](#the-settings).

1. Power returns, and macOS starts without a button press (1).
2. macOS signs the stack's user in without a password (4, 5). Nothing below starts before
   a user session exists.
3. Docker Desktop starts with that session (6).
4. The containers come back through `restart: unless-stopped` in `docker-compose.yml`.
5. The backup LaunchAgent ([`docs/backup.md`](backup.md)) loads with the session.
6. The host rejoins the network at the address the VPN route, the deploy target and every
   bookmark point to (7, 8).

What keeps it running afterwards: the machine never goes to sleep (2), and launchd fires the
03:30 backup on Berlin time (3).

## The settings

| # | Setting | Why production needs it | How to set it |
|---|---|---|---|
| 1 | **restart after power loss**: `pmset` `autorestart 1` | Without it the Mac stays off after a power cut until someone presses the button | `sudo pmset -a autorestart 1`, or System Settings → Energy → "Start up automatically after a power failure" |
| 2 | **no idle sleep**: `pmset` `sleep 0` | A sleeping host serves nothing and misses the 03:30 run; `0` turns the system sleep timer off | `sudo pmset -a sleep 0` |
| 3 | **time zone Europe/Berlin** | launchd's `StartCalendarInterval` fires on local time, and the dump names carry local timestamps | `sudo systemsetup -settimezone Europe/Berlin`, then restart (see [Notes](#launchd-and-the-time-zone)) |
| 4 | **FileVault off** | Automatic login is unavailable while FileVault is on (see [FileVault](#filevault)) | System Settings → Privacy & Security → FileVault |
| 5 | **automatic login** as the user that runs the stack | Docker Desktop and the backup agent live in that user's session | System Settings → Users & Groups → "Automatically log in as" |
| 6 | **Docker Desktop at sign-in** | Without it the session comes up and no container does. Observed on 18 October 2026 | Docker Desktop → Settings → General → "Start Docker Desktop when you sign in to your computer" |
| 7 | **hardware MAC on the network**: private Wi-Fi address off | A private address rotates the MAC the router sees, and the router then hands out another IP. Observed on 18 October 2026 | System Settings → Wi-Fi → Details on the network → Private Wi-Fi address → Off |
| 8 | **address reserved on the router** for the hardware MAC | The same address after every restart, for the VPN route, the deploy target and bookmarks | The router's DHCP settings. Not visible from the host, so `check.sh` cannot check it |

Then the running state, which the check reads as well:

| # | Check | What it proves |
|---|---|---|
| 9 | **Docker answers** | `docker info` succeeds: Docker Desktop is up |
| 10 | **web healthy** | the `web` container's health check passes. It is DB-aware, so this covers `db` as well |
| 11 | **nginx answers 401** | `http://localhost/` answers with Basic Auth: nginx is up and in front of the app |
| 12 | **backup agent loaded** | `launchctl print gui/<uid>/de.ligaauguste.planninghub.backup` finds the agent |

## The check

```sh
sh host/check.sh
```

Over SSH, from the MacBook:

```sh
ssh <host> 'cd <checkout> && sh host/check.sh'
```

One line per check, and exit status 1 if any of them failed:

```
FAIL restart after power loss: autorestart 0. Fix: sudo pmset -a autorestart 1
FAIL no idle sleep: sleep 1 (sleep prevented by caffeinate, powerd). Fix: sudo pmset -a sleep 0
ok   time zone Europe/Berlin
ok   FileVault off
ok   automatic login
ok   Docker Desktop at sign-in
ok   hardware MAC on the network
ok   Docker answers
ok   web healthy
ok   nginx answers 401
ok   backup agent loaded
```

The check only reads. It runs without `sudo`, never changes a setting, and prints the fix
for each failure instead of applying it, so it is safe to run at any time.
[#303](https://github.com/liga-auguste/planning-hub/issues/303) can call it later.

It fails closed: a value it cannot read counts as wrong. A `pmset -g` without an
`autorestart` line, an unreadable Docker settings file or a missing default route each
fail their check rather than pass it.

How each value is read, where that is not obvious:

- **No idle sleep** reads the stored `sleep` value. A host kept awake by a `caffeinate`
  process still fails: `pmset -g` then prints `sleep 1 (sleep prevented by … caffeinate)`,
  and that lasts only as long as the process. The annotation itself does not fail the
  check: `pmset` adds it whenever an assertion holds sleep off, whatever the value, so
  `sleep 0 (sleep prevented by …)` passes.
- **Time zone** reads `readlink /etc/localtime`, not `systemsetup -gettimezone`. Without
  admin rights `systemsetup` prints "You need administrator access to run this tool...
  exiting!" and still exits 0, so a check built on it would pass on a refusal.
- **Docker Desktop at sign-in** reads `AutoStart` from
  `~/Library/Group Containers/group.com.docker/settings-store.json`. Docker documents the
  file but not its keys; `AutoStart` is what Docker Desktop 4.80 writes. If an update
  renames it, the check fails loudly rather than passing. Over SSH, macOS refuses the
  read ("Operation not permitted") unless System Settings → General → Sharing → Remote
  Login → (i) → "Allow full disk access for remote users" is on; the check then fails
  with `AutoStart unreadable`. Observed on 26 October 2026, when the setting itself was
  on.
- **Hardware MAC on the network** reads the MAC of the default route's interface, so it
  stays right if the host moves to Ethernet. A private Wi-Fi address is random and
  locally administered: the IEEE 802 U/L bit, `0x02` in the first octet, is set. A
  hardware MAC never has it, and the router's reservation belongs to the hardware MAC.

## FileVault

[Apple](https://support.apple.com/en-us/HT201476): automatic login is unavailable while
FileVault is on. With FileVault on, a power cut ends at the unlock screen, and nothing
after it in the chain starts, however everything else is set. So production needs it off,
and that is a trade:

- **What it costs:** the disk is not encrypted at rest. Whoever takes the machine can read
  the database volume, the `.env` with every API key, and the backup upload key. The
  off-site copies stay safe: they are encrypted with a key that is on neither host
  ([`docs/backup.md`](backup.md)).
- **The alternative:** FileVault on, and a power cut needs someone at the machine to
  unlock it once. The check then fails on **FileVault off** by design, and the power test
  below changes from "nobody touches it" to "one unlock, nothing else".

## The power test

Proves the chain, not only the settings. Run it in daylight and away from 03:30, and
expect a few minutes of downtime.

1. Pull the Mac mini's plug while it is running, wait 30 seconds, plug it back in, and
   touch nothing.
2. About five minutes later, on the MacBook:
   `curl -s -o /dev/null -w '%{http_code}\n' http://<host>` prints `401`.
3. Run the check: every line says `ok`.
4. The next morning, `tail -3 ~/Backups/planning-hub/backup.log` shows the 03:30 run
   ending in `done`. A 03:30 timestamp means the host was awake then: launchd starts a
   missed job on wake, not at its time.

The test cuts the Mac only. A real outage takes the router down too, and the Mac then
boots before the network is back. Wi-Fi should rejoin once the router is up and the
reservation should hold, but this test does not prove either.

### Power test log

| Date | Result |
|---|---|
| 26 October 2026 | Passed. Plug pulled for 30 seconds, nothing touched: production answered `401` and every line of the check said `ok`. `caffeinate` was stopped beforehand; the 03:30 runs on 27, 28 and 29 October ended in `done`. |

## Notes

### launchd and the time zone

`man launchd.plist` does not say when launchd takes up a new time zone. Observed on 17 and
18 October 2026: after the zone was corrected, the 03:30 job still fired at 12:30, which
is 03:30 Pacific, and reloading the agent with `launchctl bootout`/`bootstrap` did not
change that. Only a restart of the host moved it to 03:30 Berlin. So a time zone change
counts as done after a restart, not before.

### `caffeinate` is not part of the setup

Sleep was held off by a `caffeinate` process started by hand, which a restart ends.
`sudo pmset -a sleep 0` is a stored setting instead. Once it is set and the check says
`ok`, a leftover `caffeinate` can be stopped:

```sh
pkill caffeinate
```

### A new host

A replacement machine needs every setting above, as well as Docker, the checkout, `.env`,
`.htpasswd` and the backup install from [`docs/backup.md`](backup.md). To run the check
over SSH, it also needs full disk access for remote users (see [The check](#the-check)).
It is done when the
check prints only `ok` lines and the power test has passed.
