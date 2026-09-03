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
