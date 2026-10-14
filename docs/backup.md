# Production backups

Implements [Issue #300](https://github.com/liga-auguste/planning-hub/issues/300), first half:
the local tier with a proven restore. The encrypted off-site copy follows in a second PR, and
the sections that depend on it say so.

Every command below runs **on the production host, in the checkout**, unless it says
otherwise. They are written to be copied in an emergency, so each one is complete.

## What a dump contains

Notion holds projects and tasks. Everything else the app knows lives only in the
PostgreSQL volume `postgres_data`:

- `PlannerRule`: the rules on `/regeln/`, which feed every planner prompt
- `WeekCloseout`: the week close-out history
- `DisplaySetting`: the chosen date style
- Django's own tables: migrations, content types, auth

A dump holds all of these. It holds the *tables* `django_session` and `planning_hub_cache`
but not their rows: sessions and cache entries are rebuilt on use.

Not in a dump, and none of it irreplaceable:

| Missing | How it comes back |
|---|---|
| `.env` | API keys are reissued, `SECRET_KEY` is regenerated, `DB_PASSWORD` is chosen anew |
| the database role's password | a fresh volume takes it from `DB_PASSWORD` in `.env` |
| `.htpasswd` | `htpasswd -c .htpasswd admin` (see the README's production section) |

## How it runs

`backup/backup.sh` does one run:

1. **Dump** with `docker compose exec -T db pg_dump -Fc`, to a `.partial` file. `-T`
   matters: without it compose allocates a TTY, and a TTY corrupts a binary stream.
2. **Prove it restores.** A throwaway `postgres:16` container (`--network none`, removed
   on every exit path) gets the dump through the [restore command](#restore) below, the
   same one this runbook uses. Then every table is checksummed on both sides, in UTC and
   with rows sorted by byte order (`COLLATE "C"`), and compared with the live database. A
   mismatch names the table. Byte order keeps the sums independent of the libc in the
   image, so a re-pulled `postgres:16` cannot fail an intact dump on `--verify`.
3. **Keep it**: the `.partial` is renamed to `planning_hub-YYYY-MM-DD_HHMMSS.dump`, and
   the restored side's per-table checksums go next to it as `<dump>.sums`. At this
   moment they equal live, and they are what `--verify` checks the dump against later.
4. **Prune** to the newest `BACKUP_KEEP` (default 90) by name, each dump with its
   `.sums`. This runs only after a successful run, and it counts dumps rather than
   measuring age: if the job fails for weeks, pruning by age would delete every good
   backup.
5. **Record it** in `last-success`: one line with a timestamp and the file name.

Any failed step exits non-zero, leaves no `.partial` behind and does not touch
`last-success`. **`last-success` is the contract with
[#303](https://github.com/liga-auguste/planning-hub/issues/303)**: once that lands, a file
older than about 26 hours raises an alert. Until then the log is the only signal.

When it runs:

- **Nightly at 03:30**, from a launchd LaunchAgent.
- **Before every production deploy.** The deploy skill runs
  `backup/backup.sh --local-only` ahead of `git pull`. A broken migration can then roll
  back to a few minutes before the deploy, not to 03:30.

`--local-only` does nothing extra yet. It will skip the off-site leg, and it is accepted
already because a deploy runs the script the host *already has*, before the pull that
would bring a newer one.

| Where | What |
|---|---|
| `~/Backups/planning-hub/planning_hub-*.dump` | the dumps, `pg_dump` custom format |
| `~/Backups/planning-hub/planning_hub-*.dump.sums` | each dump's per-table checksums, for `--verify` |
| `~/Backups/planning-hub/last-success` | the newest good run |
| `~/Backups/planning-hub/backup.log` | the LaunchAgent's output, one timestamped line per step |
| `backup/backup.env` | optional, gitignored; `BACKUP_DIR` and `BACKUP_KEEP` (see `backup/backup.env.example`) |

`~/Backups` is deliberately outside Desktop, Documents and Downloads. macOS guards those
folders per app, and a launchd job cannot answer a permission prompt.

The log grows by a few lines a night and is not rotated. At that rate it does not need to
be.

## Install

Once, on the production host:

```sh
mkdir -p ~/Library/LaunchAgents ~/Backups/planning-hub
sed -e "s|__REPO__|$PWD|" \
    -e "s|__LOG__|$HOME/Backups/planning-hub/backup.log|" \
    backup/de.ligaauguste.planninghub.backup.plist \
    > ~/Library/LaunchAgents/de.ligaauguste.planninghub.backup.plist
plutil -lint ~/Library/LaunchAgents/de.ligaauguste.planninghub.backup.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/de.ligaauguste.planninghub.backup.plist
```

If `launchctl bootstrap` is refused over SSH, run it in a Terminal on the host itself.

Then trigger one run **through launchd**, not by starting the script in Terminal:

```sh
launchctl kickstart gui/$(id -u)/de.ligaauguste.planninghub.backup
tail ~/Backups/planning-hub/backup.log
cat ~/Backups/planning-hub/last-success
```

Terminal holds file permissions of its own, so a run started there can succeed where the
scheduled one is denied. Expected: a new `.dump`, a `last-success` naming it, and a log
ending in `done`.

The agent points at the checkout, so a deploy that changes `backup/backup.sh` needs no
reinstall. A change to the plist template does:

```sh
launchctl bootout gui/$(id -u)/de.ligaauguste.planninghub.backup
# then the sed and bootstrap lines above again
```

## Restore

**One command, for every case:**

```sh
docker compose exec -T db pg_restore --clean --if-exists --create --exit-on-error -U liga -d postgres < <dump>
```

- `--create` drops the whole database and recreates it from the archive. `--clean`
  alone would drop only the objects the archive contains. A table that a broken
  migration created would survive, and the next `migrate` would fail on it with
  "already exists".
- `-d postgres` only names the database that the `DROP DATABASE` and `CREATE DATABASE`
  are issued from. The data lands in `planning_hub`.
- `--exit-on-error`, because without it pg_restore carries on past errors and reports
  only a count at the end.
- **`web` and `nginx` have to be stopped first.** `DROP DATABASE` refuses while another
  session is connected ("database is being accessed by other users"), and the app holds
  connections.

### After a broken deploy

The deploy took a dump minutes before the pull, and its report names the file (the
`kept …` line). Restore **that file by name**, not the newest one. Any run after the
deploy is newer and holds the already-migrated state: the 03:30 job when the breakage only
shows the next morning, or the pre-pull backup of a second deploy that tried to fix it.
Each of those verifies cleanly, because it faithfully captures the broken state.

If the name is lost, take the newest dump whose timestamp lies *before* the deploy:

```sh
ls -1 ~/Backups/planning-hub/planning_hub-*.dump
docker compose stop web nginx
docker compose exec -T db pg_restore --clean --if-exists --create --exit-on-error -U liga -d postgres < <dump>
```

Then roll the code back to the last good commit (the deploy skill's step 6) and start the
stack again with `docker compose up --build -d`. Restoring the data without rolling back the
code would run the broken migration again on the next start.

### After a lost volume

A `docker compose down -v`, or a damaged Docker VM disk. Start **only** `db`: `web` would
otherwise migrate and seed an empty database first.

```sh
docker compose up -d db
docker compose exec db pg_isready -h 127.0.0.1    # repeat until "accepting connections"
docker compose exec -T db pg_restore --clean --if-exists --create --exit-on-error -U liga -d postgres < <dump>
docker compose up --build -d
```

### After a lost host

This case needs the off-site copy, which comes with the second PR of #300. Until it lands,
a failed disk on the production host takes the dumps with it.

## Restore drill

A drill proves this runbook, not only the script. It runs against a **separate compose
project**, which gets its own volume.

> **Every drill command carries `-p planninghub-drill`.** Without it, `down -v` deletes the
> production volume, and the restore replaces the live database with the dump: the very
> loss this runbook exists for.

The drill compares one table's row count with the live database, so take a fresh backup
first and drill with the dump it names (the `kept …` line).

```sh
backup/backup.sh
docker compose -p planninghub-drill up -d db
docker compose -p planninghub-drill exec db pg_isready -h 127.0.0.1    # until ready
docker compose -p planninghub-drill exec -T db pg_restore --clean --if-exists --create --exit-on-error -U liga -d postgres < <dump>
docker compose -p planninghub-drill exec db psql -U liga -d planning_hub -c 'SELECT count(*) FROM projects_plannerrule'
docker compose exec db psql -U liga -d planning_hub -c 'SELECT count(*) FROM projects_plannerrule'
backup/backup.sh --verify <dump>
docker compose -p planninghub-drill down -v
```

The two counts match, and `--verify` ends in `done`. `--verify` restores the given dump
into its own throwaway and compares every table with `<dump>.sums`, the checksums
recorded when the dump was taken. It does not look at the live data, so it works for a
dump of any age, and it writes nothing. A dump without its `.sums` is refused.

A normal run compares with the live database instead: a write between the dump and the
live checksum (the throwaway's start and the restore lie in between) makes that run report
a mismatch. That includes the run before a deploy. Running it again resolves it.

### Drill log

| Date | Dump | Result |
|---|---|---|
| | | No drill yet. The first one runs after the first scheduled backup. |
