# Production monitoring

Implements [Issue #303](https://github.com/liga-auguste/planning-hub/issues/303).

Before this, production had no signal of its own. An unhandled exception became a 500
nobody heard about, a failed Notion or Claude call was a console line, the backup could
fail silently, and every deploy dropped the `web` container's logs along with the
container. On 18 October 2026 production was down after a restart until someone happened
to look.

`host/watch.sh` runs on the production host every five minutes. Each run:

1. **collects** the new `web` and `nginx` log lines into files on the host, which outlive
   every container;
2. **checks** the host with `host/check.sh` ([`docs/production-host.md`](production-host.md))
   and the backup's `last-success` ([`docs/backup.md`](backup.md));
3. **counts app errors** in the `web` lines it just collected;
4. **pings** three [Healthchecks.io](https://healthchecks.io/docs/) checks with the result.

Healthchecks.io is a dead man's switch: it alerts on an explicit `/fail` ping, and also
when the pings stop. That covers the one failure the host cannot report itself: host
down, Docker Desktop not started, no network. How an alert reaches the maintainer (email,
push, messenger) is set in Healthchecks.io and needs no code.

Every command below runs **on the production host, in the checkout**, unless it says
otherwise.

## The three checks

| Check | Schedule | Goes down when |
|---|---|---|
| **production** | simple, period 5 min, grace 10 min | a host check fails twice in a row, or the pings stop |
| **backup** | simple, period 1 day, grace 1 day | `last-success` is stale. Missing pings are the production check's job; this one goes down for them only after two days without any |
| **errors** | simple, period 1 day, grace 1 day | a run counts app errors. Missing pings: as for backup |

Three checks rather than one, because Healthchecks.io notifies only when a check changes
state: a `/fail` or a missed ping on a check that is already down sends nothing. A stale
backup keeps its check down until the next good night, which can be a day or more; on a
shared check it would hide every host failure and every burst of 500s in that time.

## What each alert means

A `/fail` ping carries a short body, one line per finding:

| Line | Check | Meaning | Where to look |
|---|---|---|---|
| `FAIL <label>`, e.g. `FAIL web healthy` | production | that row of `host/check.sh` failed on two runs in a row | `sh host/check.sh` prints the found value and the fix. The labels are listed in [`docs/production-host.md`](production-host.md). An unreadable Docker settings file (row 6) is left out, see below |
| `FAIL host check` | production | `check.sh` failed twice without printing a `FAIL` line, so it did not run at all | `watch.log` |
| `FAIL backup fresh` | backup | `last-success` is older than 26 hours or missing: a nightly backup was missed | `~/Backups/planning-hub/backup.log` |
| `<n> unhandled errors (500)` | errors | `n` unhandled exceptions since the last run. One is enough | `web.log`, search for `Internal Server Error:`; the traceback follows the line |
| `<n> Notion failures` | errors | at least three failed Notion calls since the last run: an outage, or a broken key | `web.log`, search for `Notion call failed:` |
| `<n> Claude failures` | errors | at least three failed Claude calls since the last run | `web.log`, search for `outcome=error` |

**Why one run is not enough for a host check.** A deploy rebuilds `web` within about a
minute, and after a power cut the host is back within about five minutes (power test, 26
October 2026). Neither should page anyone, so a failing `check.sh` alerts on the second
run in a row: about ten minutes into a real outage. A stale backup alerts on the first
run, since 26 hours is already late. App errors alert on the first run too: they are
events, not states.

**Why an unreadable row 6 does not alert.** Row 6 reads Docker Desktop's settings from a
folder macOS guards per app. Terminal holds a permission of its own, but over SSH the read
is refused ("Operation not permitted", [`docs/production-host.md`](production-host.md)),
and a launchd job cannot answer the permission prompt. That refusal says nothing about the
host, and every run would fail on it: from the second run on the production check would
stay down for good, and no later outage would notify. So the watcher leaves out exactly
the `AutoStart unreadable` finding. A setting found switched off still alerts, and
`sh host/check.sh` in a Terminal still reports the unreadable file.

**Why one Notion or Claude failure is not enough.** A single failure is something the user
sees in the interface, with its own message. Three within five minutes are an outage or a
broken key. The views answer those failures with a `502`, which Django logs as
`Bad Gateway:`. Those lines are not counted, or each failure would count twice.

**Why the body has no details.** A ping body carries only labels and counts: never a log
line, a found value or a traceback. Django's error-reporting docs warn against sending
error reports unencrypted over the internet, and Healthchecks.io is a third party. The
details are on the host, in the collected logs.

## The logs

| Path | What |
|---|---|
| `~/Library/Logs/planning-hub/web.log` | the app's output: requests that failed, the `projects` logger (every Claude call, every Notion failure), tracebacks |
| `~/Library/Logs/planning-hub/nginx.log` | nginx's access and error log |
| `~/Library/Logs/planning-hub/watch.log` | the watcher's own output: each run, `check.sh`'s lines, the alerts it sent |
| `~/Library/Logs/planning-hub/.web.cursor`, `.nginx.cursor` | the timestamp of the newest stored line, so the next run stores only what is newer |
| `~/Library/Logs/planning-hub/.check-failed` | present while the last run's `check.sh` failed (the second-run rule) |
| `~/Library/Logs/planning-hub/.lock` | present while a run is in progress; it names that run's process |

Every line starts with Docker's UTC timestamp. Each file rotates at 10 MB to `.1` … `.5`,
so a file family holds at most 60 MB. At an estimated 1.5 MB a day that reaches back
weeks, not days.

They reach back to the first run on this host. On 29 October 2026 the `web` logs began at
that morning's deploy, while nginx, which no deploy had recreated, still held its log
back to 20 August. The first run copies all of it.

**Inside the containers**, both compose files bound every service's logs with Docker's
`local` driver, `max-size: 10m` and `max-file: 5`. The default `json-file` driver never
rotates. Those logs still go when a container is recreated; the files above do not.

**How a 500 reaches the log.** Django sends `django.request` to the console only with
`DEBUG=True`. `settings.py` routes it to the `console` handler at `ERROR`, so in
production every 500 leaves a line with its traceback.

## Install

Once, on the production host.

### 1. The checks in Healthchecks.io

1. Sign up at [healthchecks.io](https://healthchecks.io/) (the free plan has 20 checks).
2. Create a check named **production**: schedule "Simple", period 5 minutes, grace time 10
   minutes.
3. Create a check named **backup**: schedule "Simple", period 1 day, grace time 1 day.
4. Create a check named **errors**: schedule "Simple", period 1 day, grace time 1 day.
5. Under Integrations, add the channel the alerts should reach (email, ntfy, Telegram,
   Signal, …) and enable it for all three checks.
6. Copy the three ping URLs.

### 2. The settings file

```sh
cp host/watch.env.example host/watch.env
```

Set `WATCH_PING_URL` to the production check's URL, `WATCH_BACKUP_PING_URL` to the backup
check's and `WATCH_ERRORS_PING_URL` to the errors check's. A ping URL is a secret: whoever has it can report the check up or down.
`host/watch.env` is gitignored and stays on the host.

### 3. A first run by hand

```sh
sh host/watch.sh
ls -la ~/Library/Logs/planning-hub/
```

Expected: all three checks turn "up" in Healthchecks.io, and the folder holds `web.log` and
`nginx.log`. Row 13, `watch agent loaded`, fails until step 4 has installed the agent. The
first run only notes that, so run it once here: a second run before step 4 would send
`FAIL watch agent loaded`. Exit status 1 means the watcher could not do its own job, and the line it
printed says which part: settings missing, logs unreadable, or a ping not delivered.

### 4. The schedule

```sh
mkdir -p ~/Library/LaunchAgents ~/Library/Logs/planning-hub
sed -e "s|__REPO__|$PWD|g" \
    -e "s|__LOG__|$HOME/Library/Logs/planning-hub/watch.log|" \
    host/de.ligaauguste.planninghub.watch.plist \
    > ~/Library/LaunchAgents/de.ligaauguste.planninghub.watch.plist
plutil -lint ~/Library/LaunchAgents/de.ligaauguste.planninghub.watch.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/de.ligaauguste.planninghub.watch.plist
```

If `launchctl bootstrap` is refused over SSH, run it in a Terminal on the host itself.

Then one run **through launchd**, since Terminal holds file permissions of its own:

```sh
launchctl kickstart gui/$(id -u)/de.ligaauguste.planninghub.watch
tail ~/Library/Logs/planning-hub/watch.log
sh host/check.sh
```

Expected: `watch.log` ends with a run whose `check.sh` lines all say `ok`, and the check
prints `ok   watch agent loaded`.

The agent has no `RunAtLoad`. After a reboot the first run comes one interval after
login, which is inside the production check's grace time.

The agent points at the checkout, so a deploy that changes `host/watch.sh` needs no
reinstall. A change to the plist template does:

```sh
launchctl bootout gui/$(id -u)/de.ligaauguste.planninghub.watch
```

and then the `sed` and `bootstrap` lines above again.

## Deploys

The deploy skill runs `sh host/watch.sh --no-host-check` before it pulls, so the logs up to
the recreate are collected and counted. The flag skips `host/check.sh` and leaves the
second-run rule alone: over SSH a row can fail on a healthy host, and that failure would
make the LaunchAgent's next run, which may land mid-recreate, the second in a row. So a
deploy does not page. Lines `web` writes between that run and the recreate, during the
build, are lost with the container.

Only one run works at a time. A run that finds another one holding the lock waits for it,
at most a minute, and then exits 1 without pinging. A lock left behind by a run that is
gone, after a power cut say, is taken over.

## Drills

Prove the alert path end to end, and record each run below.

1. **Alert drill.** Run it once with a backup folder that does not exist:

   ```sh
   BACKUP_DIR=/nonexistent sh host/watch.sh
   ```

   A "down" notification for backup with `FAIL backup fresh` arrives. A normal
   `sh host/watch.sh` afterwards sets it "up" again.
2. **Missed-ping drill.** Unload the agent and run nothing for 20 minutes:

   ```sh
   launchctl bootout gui/$(id -u)/de.ligaauguste.planninghub.watch
   ```

   A "down" notification without a body arrives, from the missing pings. Bootstrap the
   agent again as in [The schedule](#4-the-schedule), and production goes "up".

### Drill log

| Date | Drill | Result |
|---|---|---|

## Known limits

- **A short outage is not reported.** The second-run rule means anything that heals within
  about ten minutes goes unreported, by design.
- **No live 500 drill.** The app has no endpoint that fails on purpose, and adding one is
  not worth it. The 500 path is proven by the tests instead: `test_monitoring.py` builds
  the stubbed `web` log from the app's real log records, so a changed log message fails
  the suite, not production.
- **Gunicorn's `[CRITICAL] WORKER TIMEOUT` lines** are collected but not counted.
- **The client address.** Every request in `nginx.log` carries Docker Desktop's internal
  gateway, `192.168.65.1`, not the client's address. See
  [Network boundary](production-readiness.md#network-boundary).
- **Healthchecks.io is a third party.** It sees how often the host pings, the labels and
  the counts. It sees no log content, no addresses and no task names. If it is down,
  alerts are late, not false.
- **The demo stack is not watched.** It is public and holds no real data; it gets only the
  size bounds on its Docker logs.
