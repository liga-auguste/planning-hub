# Production backups

Implements [Issue #300](https://github.com/liga-auguste/planning-hub/issues/300). Two tiers,
one script:

- **Local**, on the production host: a dump every night and before every deploy, each one
  proven to restore before it is kept. It covers a broken migration, a `docker compose
  down -v` and a damaged Docker VM disk, without any network.
- **Off-site**, on the demo VPS: the same dump, encrypted with
  [age](https://github.com/FiloSottile/age). It covers what takes the production host with
  it: a failed disk, theft, fire. Neither host holds the key that decrypts it.

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
4. **Send it off-site.** `age -r <recipient>` encrypts the dump to a temporary file,
   which is checked and then sent with a plain `ssh … < file` to the backup user on the
   VPS. That user's key can do one thing: run the receiver (`backup/receive.sh`), which
   stores stdin under a name taken from **the VPS's own clock, in UTC**, so the off-site
   name is not the local one. The run logs both (`encrypting …`, `off-site: stored …`).
   No ciphertext stays on the production host.
5. **Prune** to the newest `BACKUP_KEEP` (default 90) by name, each dump with its
   `.sums`. This runs only after a successful run, and it counts dumps rather than
   measuring age: if the job fails for weeks, pruning by age would delete every good
   backup.
6. **Record it** in `last-success`: one line with a timestamp and the file name.

Any failed step exits non-zero, leaves no `.partial` behind and does not touch
`last-success`. A failed upload keeps the local dump: the local tier is good, only the
off-site one is missing. A missing off-site setting fails the same way, after the local
dump is kept, and names the setting. **`last-success` is the contract with the
watcher** ([#303](https://github.com/liga-auguste/planning-hub/issues/303)): a file older
than 26 hours, or none at all, sends a `FAIL backup fresh` alert within five minutes. See
[`docs/monitoring.md`](monitoring.md).

When it runs:

- **Nightly at 03:30**, from a launchd LaunchAgent.
- **Before every production deploy.** The deploy skill runs
  `backup/backup.sh --local-only` ahead of `git pull`. A broken migration can then roll
  back to a few minutes before the deploy, not to 03:30.

The deploy's run is `--local-only`: dump, prove, keep, prune, no off-site copy. It
**leaves `last-success` alone**, because that file promises an off-site copy too; a deploy
must not refresh it and hide a nightly off-site leg that is failing. The option has been
accepted since the first version, because a deploy runs the script the host *already has*,
before the pull that would bring a newer one.

| Where | What |
|---|---|
| `~/Backups/planning-hub/planning_hub-*.dump` | the dumps, `pg_dump` custom format |
| `~/Backups/planning-hub/planning_hub-*.dump.sums` | each dump's per-table checksums, for `--verify` |
| `~/Backups/planning-hub/last-success` | the newest good run |
| `~/Backups/planning-hub/backup.log` | the LaunchAgent's output, one timestamped line per step |
| `backup/backup.env` | gitignored; `BACKUP_DIR` and `BACKUP_KEEP` (optional), the three off-site settings (required without `--local-only`); see `backup/backup.env.example` |
| VPS: `/home/planninghub-backup/planning-hub/planning_hub-*.dump.age` | the off-site copies; the receiver prunes only what is beyond the newest 90 *and* older than 90 days |
| VPS: `/usr/local/bin/planninghub-receive` | the receiver, a root-owned copy of `backup/receive.sh` |

`~/Backups` is deliberately outside Desktop, Documents and Downloads. macOS guards those
folders per app, and a launchd job cannot answer a permission prompt.

The log grows by a few lines a night and is not rotated. At that rate it does not need to
be.

## Install

### The schedule

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

### The off-site copy

Do this in one session, before the next 03:30 run. Once the deploy has brought the
off-site leg, a nightly run without the settings below fails closed, as designed.

**1. The age key pair, on the maintainer's MacBook.** The identity (the private key) never
goes to either host:

```sh
mkdir -p ~/.config/age
age-keygen -o ~/.config/age/planninghub-backup.txt
```

It prints `Public key: age1…`: that is the recipient.

Store the identity file's content in the password manager as well. Without it the
off-site copies cannot be read by anyone, so the MacBook alone is one disk away from
losing them.

**2. On the production host**: `age`, an SSH key for the upload, and the VPS's host key.

```sh
brew install age
ssh-keygen -t ed25519 -N '' -C planninghub-backup -f ~/.ssh/planninghub_backup_ed25519
ssh-keyscan -t ed25519 <vps> > ~/vps-hostkey
ssh-keygen -lf ~/vps-hostkey
```

On the VPS, as root, print the fingerprint the host itself holds:

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

Only if the two fingerprints match, pin it and remove the scratch file:

```sh
cat ~/vps-hostkey >> ~/.ssh/known_hosts && rm ~/vps-hostkey
```

A first connection cannot store it instead: `BatchMode=yes` refuses an unknown host key,
and an interactive login with the restricted key would only run the receiver.

Then `backup/backup.env`, from `backup/backup.env.example`, with the three off-site
settings: the recipient from step 1, `planninghub-backup@<vps>`, and
`$HOME/.ssh/planninghub_backup_ed25519`.

**3. Deploy both stacks**, demo first: its pull brings `backup/receive.sh` into the VPS
checkout.

**4. On the VPS, as root**: the user, the receiver and the one key.

```sh
adduser --disabled-password --gecos '' --shell /bin/sh planninghub-backup
install -o root -g root -m 0755 <checkout>/backup/receive.sh /usr/local/bin/planninghub-receive
install -d -o root -g root -m 0755 /home/planninghub-backup/.ssh
printf 'restrict,command="/usr/local/bin/planninghub-receive" %s\n' '<contents of planninghub_backup_ed25519.pub>' \
    > /home/planninghub-backup/.ssh/authorized_keys
chmod 0644 /home/planninghub-backup/.ssh/authorized_keys
```

- The user gets a real login shell. sshd runs a forced command through the user's shell
  with `-c`, so with `nologin` the receiver would never run. The restriction is the
  `authorized_keys` line: `command=` replaces whatever the client asks to run, and
  `restrict` turns off port forwarding, agent forwarding and the PTY. Without a PTY the
  channel stays 8-bit clean for the binary upload.
- `.ssh` and `authorized_keys` belong to root, so not even the receiver could change
  which key gets in.
- The receiver is a **copy**. A change to `backup/receive.sh` needs the `install` line
  again; a deploy does not do it.
- The upload key sits on the production host without a passphrase, so whoever takes
  that host can upload too. The receiver therefore never prunes a copy younger than 90
  days, however many uploads follow it, and refuses an upload once it holds 400 copies
  or 1 GiB. A flood of uploads then fails the nightly run instead of evicting the
  history. If that happens, find out why before deleting anything on the VPS: the junk
  is the newest files, the real copies are the ones the log's `off-site: stored …`
  lines name.

**5. Trigger a run through launchd** (see above) and check both sides:

```sh
tail ~/Backups/planning-hub/backup.log
cat ~/Backups/planning-hub/last-success
ssh root@<vps> 'ls -l /home/planninghub-backup/planning-hub'
```

The log ends in `off-site: stored planning_hub-….dump.age`, then `done`.

Then run the [off-site drill](#off-site-drill).

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
docker compose exec db pg_isready -h 127.0.0.1
docker compose exec -T db pg_restore --clean --if-exists --create --exit-on-error -U liga -d postgres < <dump>
docker compose up --build -d
```

Repeat `pg_isready` until it prints `accepting connections` before restoring.

### After a lost host

The local dumps are gone with it; the off-site copies are not. On the maintainer's MacBook,
fetch the newest one and decrypt it with the identity:

```sh
ssh root@<vps> 'ls -1 /home/planninghub-backup/planning-hub | tail -1'
scp root@<vps>:/home/planninghub-backup/planning-hub/<name>.dump.age .
age -d -i ~/.config/age/planninghub-backup.txt -o planning_hub.dump <name>.dump.age
```

On the new host: Docker, the checkout and a new `.env` (see [What a dump
contains](#what-a-dump-contains)), then the same steps as [after a lost
volume](#after-a-lost-volume) with `planning_hub.dump`. Install the schedule and the
off-site copy again afterwards; the new host needs a new upload key. Its macOS settings
are in [`docs/production-host.md`](production-host.md): without them it does not come back
after a power cut.

## Restore drill

A drill proves this runbook, not only the script. It runs against a **separate compose
project**, which gets its own volume.

> **Every drill command carries `-p planninghub-drill`.** Without it, `down -v` deletes the
> production volume, and the restore replaces the live database with the dump: the very
> loss this runbook exists for.

The drill compares one table's row count with the live database, so take a fresh backup
first and drill with the dump it names (the `kept …` line).

```sh
backup/backup.sh --local-only
docker compose -p planninghub-drill up -d db
docker compose -p planninghub-drill exec db pg_isready -h 127.0.0.1
docker compose -p planninghub-drill exec -T db pg_restore --clean --if-exists --create --exit-on-error -U liga -d postgres < <dump>
docker compose -p planninghub-drill exec db psql -U liga -d planning_hub -c 'SELECT count(*) FROM projects_plannerrule'
docker compose exec db psql -U liga -d planning_hub -c 'SELECT count(*) FROM projects_plannerrule'
backup/backup.sh --verify <dump>
docker compose -p planninghub-drill down -v
```

Repeat `pg_isready` until it prints `accepting connections` before restoring. The two
counts match, and `--verify` ends in `done`. `--verify` restores the given dump
into its own throwaway and compares every table with `<dump>.sums`, the checksums
recorded when the dump was taken. It does not look at the live data, so it works for a
dump of any age, and it writes nothing. A dump without its `.sums` is refused.

A normal run compares with the live database instead: a write between the dump and the
live checksum (the throwaway's start and the restore lie in between) makes that run report
a mismatch. That includes the run before a deploy. Running it again resolves it.

## Off-site drill

Proves that an off-site copy can be read with a key that lives on neither host, and that
what comes out is the dump that was verified. Run it right after a full run, so the newest
file on each side belongs to the same run; the log names the pair (`encrypting <dump>`,
then `off-site: stored <name>.dump.age`).

On the maintainer's MacBook:

```sh
scp root@<vps>:/home/planninghub-backup/planning-hub/<name>.dump.age .
age -d -i ~/.config/age/planninghub-backup.txt -o offsite.dump <name>.dump.age
scp offsite.dump <production-host>:
```

On the production host, `--verify` the decrypted file against the checksums its local
twin recorded. The `.sums` stay on the production host and are not sent off-site, so the
drill borrows them:

```sh
cp ~/Backups/planning-hub/<dump>.sums ~/offsite.dump.sums
backup/backup.sh --verify ~/offsite.dump
cmp ~/offsite.dump ~/Backups/planning-hub/<dump> && rm ~/offsite.dump ~/offsite.dump.sums
```

`--verify` ends in `done` and `cmp` prints nothing. Delete the plain dump on the MacBook
as well: it is the one unencrypted copy outside the production host.

### Drill log

| Date | Drill | Dump | Result |
|---|---|---|---|
| 2026-10-17 | Off-site | `planning_hub-2026-10-16_155330.dump`, sent as `planning_hub-2026-10-16_225334.dump.age` | Passed. Decrypted on the MacBook, `--verify` matched 14 tables, `cmp` identical. First run through real `age`, `ssh` and the receiver. |
| 2026-10-17 | Restore | `planning_hub-2026-10-17_132822.dump` | Passed. Restored into `-p planninghub-drill` with the runbook's command, `projects_plannerrule` 4 rows on both sides, `--verify` matched 14 tables; drill volume removed, production volume and containers untouched. |
