# Deploy hosts (template)

Real SSH targets and paths live in `hosts.md` (gitignored, holds real
infrastructure details). Create it once:

```bash
cp .claude/skills/deploy/hosts.example.md .claude/skills/deploy/hosts.md
```

Then fill in the placeholders below with the real values.

## demo

- SSH: `ssh user@demo-host`
- Path: `path/to/planning-hub-demo` (relative to the SSH user's home)
- URL: `https://demo.example.com`
- Compose file: `docker-compose.demo.yml`

## production

- SSH: `ssh user@production-host`
- Path: `path/to/planning-hub`
- URL: `http://production-host` (VPN-only)
- Compose file: `docker-compose.yml`
- Shell: *(optional, and available for either stack — it is written here
  because it is the likelier one)* a wrapper the docker command runs through
  on this host, e.g. `zsh -lc`. Needed only when `docker` is missing from the
  PATH of a non-interactive SSH session — check with
  `ssh user@production-host 'which docker'` against
  `ssh user@production-host 'zsh -lc "which docker"'`. Omit the line
  entirely when the plain call finds it.
