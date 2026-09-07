---
name: deploy
description: >-
  Deploy the demo and production stacks after a merge to main. Checks for
  local drift, pulls, rebuilds, and verifies the live site responds. Use
  when deploying, pushing a release live, or updating one or both of the
  two running stacks. The maintainer runs this deliberately: with no
  argument it reaches the production stack, so offer it when a deploy
  looks due rather than starting one.
argument-hint: "[d|p|both] — empty deploys both"
allowed-tools: Bash(ssh *) Bash(git status) Bash(git diff *) Bash(git log *) Bash(git checkout *) Bash(docker compose *) Bash(curl *) Read
---

# Deploy — Deploy a Stack

Deploy the demo and production stacks and verify they came up.

## Context

- Stacks: `$ARGUMENTS` — see step 1. Empty means both.
- Host details: `.claude/skills/deploy/hosts.md` (gitignored — real SSH
  targets, paths, URLs, and anything else that is true of one machine
  rather than of this project). If it doesn't exist yet, create it first:
  `cp .claude/skills/deploy/hosts.example.md .claude/skills/deploy/hosts.md`
  and fill in the placeholders.

## Steps

### 1. Resolve the target

Deploying both is the common case, so that is what an empty argument
means:

| Argument | Stacks |
|---|---|
| *(empty)*, `b`, `both`, `all` | demo, then production |
| `d`, `demo` | demo only |
| `p`, `prod`, `production` | production only |

Anything else is a typo, not a stack — ask rather than guess. The two
stacks run different databases (SQLite vs PostgreSQL) on different data
(fixture vs real), so a wrong guess is not a safe default.

**The order is not cosmetic.** Demo goes first because it is the public,
fixture-backed stack: if the merge doesn't build or the app doesn't come
up, that is the stack to find it on. Production starts only once demo has
come through step 5 clean — see step 2 for what to do when it doesn't.

Nothing here deploys your local checkout. Both hosts pull into their own
clone, so the branch you happen to be on locally makes no difference — to
deploy something that isn't on `main` yet, this is the wrong tool.

That a host lands on `main` is a fact about that host, not something this
skill enforces: `git pull` takes whatever branch the host is already on.
Step 2 checks it, because step 6 can leave a host somewhere else.

### 2. Preconditions

Run steps 2 to 5 for one stack, then the next. Don't interleave them: a
half-deployed pair is the state that is hardest to read afterwards.

**On a both-stacks run, production starts only once demo has come through
steps 2 to 5 cleanly** — not merely once step 5 has answered. Anything
that stops demo short of that ends the run: a checkout that can't be
safely pulled over, a `git pull` that refuses, a build that doesn't come
up. Demo goes first precisely so that a bad merge is caught before
production gets it, and a bad merge usually fails at step 3, which never
reaches step 5's check at all. Whatever the stop, report which stack sits
on which commit before anything else. That is the fact the next decision
needs, and the one that is easiest to lose halfway through a pair of
deploys.

Read the target stack's entry in `hosts.md` for its SSH target, path, URL,
compose file, branch, and its optional `Shell` (step 3). Required `.env`
keys on the host, already documented in the README's "Docker (demo)" /
"Docker (production)" sections:

- **demo**: `DEMO_MODE=true`, `ALLOWED_HOSTS`, `SECRET_KEY`, `ANTHROPIC_API_KEY`
- **production**: `DEMO_MODE=false`, `SECRET_KEY`, `ANTHROPIC_API_KEY`, `NOTION_API_KEY`, `DB_PASSWORD`, `DB_HOST`

Check the host's checkout before pulling. Two different things can be
wrong with it, and one command shows both:

```bash
ssh <host> 'cd <path> && git status'
```

- **Uncommitted edits.** One has silently diverged from `main` before (the
  `.htpasswd` mount, #187/#188). Resolve or stash anything found; do not
  pull over it.
- **`HEAD detached at <commit>` on the first line.** A step 6 rollback that
  was never undone. This is the half that is easy to read past, because
  the rest of the output still says the working tree is clean. Do not pull
  over it either: `git pull` refuses on a detached HEAD and exits
  non-zero, which short-circuits the `&&` in step 3 and skips the rebuild
  entirely, leaving the host on the rollback commit while the run reads as
  if it did something. Put the host back on the branch its `hosts.md`
  entry names — `git checkout <branch>` — once you know why the rollback
  was still there.

### 3. Deploy

```bash
ssh <host> 'cd <path> && git pull && docker compose -f <compose-file> up --build -d'
```

`<compose-file>` is `docker-compose.demo.yml` for demo, `docker-compose.yml`
for production (see `hosts.md`).

**If the stack's `hosts.md` entry names a `Shell`, the docker half runs
through it**, as two commands rather than one:

```bash
ssh <host> 'cd <path> && git pull'
ssh <host> '<shell> "cd <path> && docker compose -f <compose-file> up --build -d"'
```

`docker` is not always on the PATH of a *non-interactive* SSH session — a
host whose PATH entry comes from the user's login profile (a common Docker
Desktop setup) will not have it. The failure is worse than it looks: the
pull in the combined command has already landed when `docker compose`
reports `command not found`, so the checkout sits ahead of the running
containers and the deploy looks done from the outside. Splitting the two
keeps that from being silent.

Leave the wrapper off for any stack whose `hosts.md` entry has no `Shell`.

### 4. Stack-specific gotchas

- **demo**: HTTPS certificate renewal is a latent problem, verified on the
  host on 2026-09-03. The `certbot.timer` systemd unit (twice daily, the
  distro package default — no crontab entry) runs `certbot renew` with
  `authenticator = standalone`, which binds ports 80/443 itself while it
  runs. But `docker-compose.demo.yml` maps those same host ports to the
  nginx container, and `/etc/letsencrypt/renewal-hooks/{pre,deploy,post}`
  are all empty — nothing stops the stack first. Every renewal run logged
  so far has been a no-op ("not yet due"); the cert expires 2026-11-02, so
  the first real attempt (30 days out, ~2026-10-03) is likely to fail on
  a port-bind conflict, with the demo running on an expiring cert
  afterward. Not fixed here — tracked in #202: either switch the
  authenticator to work through the running container (webroot/nginx
  plugin) or add a pre/deploy hook that stops/restarts compose around the
  renewal.
- **production**: `.htpasswd` must exist on the host before the first
  start, or nginx fails in a way that doesn't say "no such file" — see the
  README's "Docker (production)" section for why and how to create it.
  Not duplicated here.

### 5. Verify

```bash
curl -o /dev/null -s -w "%{http_code}\n" <url>
```

Expect `200` for demo (public). Expect `401` for production (Basic Auth,
no credentials supplied) — that confirms nginx is serving and auth is
enforced, not that the app itself is healthy.

An unexpected code is one of step 2's stops. Reached after demo, it means
production never starts and keeps serving its previous build — which is
where you want it while a bad build is still unexplained.

### 6. Rollback

Roll back only the stack that is actually broken — the two are independent
deploys and a healthy one is not evidence about the other.

The only rollback that exists today: check out the previous commit on the
host and rebuild. Database backup/restore is out of scope (tracked
separately).

```bash
ssh <host> 'cd <path> && git log --oneline -5'   # find the last-known-good commit
ssh <host> 'cd <path> && git checkout <commit> && docker compose -f <compose-file> up --build -d'
```

On a stack with a `Shell`, split the second line the way step 3 does. A
rollback is the worst moment for a checkout that lands while the rebuild
does not: the host would sit on the older commit with the containers still
running the build you are rolling back.

```bash
ssh <host> 'cd <path> && git checkout <commit>'
ssh <host> '<shell> "cd <path> && docker compose -f <compose-file> up --build -d"'
```

A rollback leaves the host on a detached HEAD, and it stays there until
someone puts it back. That is deliberate — the next deploy refuses to pull
rather than quietly rolling forward over an unexplained rollback (step 2)
— but it does mean the stack is outside the normal deploy path until you
run `git checkout <branch>` on it on purpose, with `<branch>` from its
`hosts.md` entry.
