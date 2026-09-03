---
name: deploy
description: >-
  Deploy the demo or production stack after a merge to main. Checks for
  local drift, pulls, rebuilds, and verifies the live site responds. Use
  when deploying, pushing a release live, or updating one of the two
  running stacks.
argument-hint: "[demo|production]"
allowed-tools: Bash(ssh *) Bash(git status) Bash(git diff *) Bash(git log *) Bash(git checkout *) Bash(docker compose *) Bash(curl *) Read
---

# Deploy — Deploy a Stack

Deploy the demo or production stack and verify it came up.

## Context

- Stack: `$ARGUMENTS` (`demo` or `production`)
- Host details: `.claude/skills/deploy/hosts.md` (gitignored — real SSH
  targets, paths, URLs). If it doesn't exist yet, create it first:
  `cp .claude/skills/deploy/hosts.example.md .claude/skills/deploy/hosts.md`
  and fill in the placeholders.

## Steps

### 1. Confirm the target stack

If `$ARGUMENTS` doesn't name `demo` or `production`, ask before doing
anything else. The two stacks run different databases (SQLite vs
PostgreSQL) on different data (fixture vs real) — guessing wrong is not a
safe default.

### 2. Preconditions

Read the target stack's entry in `hosts.md` for its SSH target, path, and
URL. Required `.env` keys on the host, already documented in the README's
"Docker (demo)" / "Docker (production)" sections:

- **demo**: `DEMO_MODE=true`, `ALLOWED_HOSTS`, `SECRET_KEY`, `ANTHROPIC_API_KEY`
- **production**: `DEMO_MODE=false`, `SECRET_KEY`, `ANTHROPIC_API_KEY`, `NOTION_API_KEY`, `DB_PASSWORD`, `DB_HOST`

Check for local drift on the host before pulling — an uncommitted edit
there has silently diverged from `main` before (the `.htpasswd` mount,
#187/#188):

```bash
ssh <host> 'cd <path> && git status'
```

Resolve or stash anything found; do not pull over it.

### 3. Deploy

```bash
ssh <host>
cd <path>
git pull
docker compose -f <compose-file> up --build -d
```

`<compose-file>` is `docker-compose.demo.yml` for demo, `docker-compose.yml`
for production (see `hosts.md`).

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

### 6. Rollback

The only rollback that exists today: check out the previous commit on the
host and rebuild. Database backup/restore is out of scope (tracked
separately).

```bash
ssh <host>
cd <path>
git log --oneline -5   # find the last-known-good commit
git checkout <commit>
docker compose -f <compose-file> up --build -d
```
