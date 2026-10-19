# Production Readiness

Implements [Issue #27](https://github.com/liga-auguste/planning-hub/issues/27).

## Context

Five small things that individually were each too small to justify their own issue,
but collectively are the difference between "running" and "deployed": a favicon,
social preview tags, styled error pages, a `robots.txt` and a `/health/` endpoint.
All five touch the same two files (`base_public.html` / `base_dashboard.html`) or the
demo nginx config, which is why they're grouped here.

## Decisions

### robots.txt: disallow all

The demo (`planninghub.ligaauguste.de`) is a portfolio piece, not a product meant to
rank in search — so `nginx-demo.conf` now serves `User-agent: * / Disallow: /`
directly, with no Django round-trip. Production sits behind HTTP Basic Auth already,
which makes a `robots.txt` there moot, so `nginx.conf` is untouched. If the demo is
already indexed by the time this ships, `Disallow: /` only affects *future* crawling —
removing existing results needs a separate Google Search Console request.

### /health/: DB-aware in production, pure liveness in demo

The demo stack has no Postgres service (it runs on SQLite), so its health check can
only ever be a liveness probe. Production does have a database, and a `web` container
that's up but can't reach it is exactly the kind of "successful but not serving"
state `docker compose up -d` was hiding — so the same view branches on `DEMO_MODE`
rather than shipping two separate endpoints.

The check deliberately avoids `curl`/`wget` in the compose healthcheck command —
`python:3.12-slim` has neither preinstalled, but Python itself is always there:

```yaml
healthcheck:
  test: ["CMD", "python", "-c", "import urllib.request as u; u.urlopen('http://localhost:8000/health/', timeout=3)"]
```

The other half of the fix is `nginx`'s `depends_on` moving from the short form to
`condition: service_healthy` in both compose files — a bare `healthcheck:` only makes
status visible in `docker compose ps`, it doesn't by itself stop nginx from routing to
a `web` container that isn't ready yet.

**Operational gotcha, found on the first real demo deploy:** the healthcheck command
requests `http://localhost:8000/health/`, and Django validates the `Host` header
against `ALLOWED_HOSTS` before the request ever reaches the view. The example env
file has always included `localhost` there (one per stack since #301:
`.env.demo.example` and `.env.production.example`), but the already-deployed demo host's `.env`
predated this feature and had `ALLOWED_HOSTS` set to only its public hostname —
so the check failed with a 400 and `web` reported `unhealthy`, even though the app
itself was serving correctly (nginx still resolved the `web` service by name).
Fixed by adding `localhost` to that host's `ALLOWED_HOSTS`; see README's Docker
sections for the standing requirement.

### og-image.png as its own static copy

`og:image` reuses the dashboard screenshot from #26, but as a dedicated copy under
`projects/static/projects/og-image.png` rather than a reference into `docs/`. The
app's static pipeline (`collectstatic`, content hashing) shouldn't depend on a
directory GitHub renders separately and that isn't guaranteed to exist in every
checkout shape.

## Network boundary

Added in [#301](https://github.com/liga-auguste/planning-hub/issues/301). Production
opts out of HTTPS (`HTTPS_ONLY=false`, see the README's "Docker (production)"
section), and that opt-out is only sound because of where the stack can be reached
from. This section writes that down.

**The VPN is the only route from outside.** A WireGuard VPN that ends at the home
router is the one way to reach the production host from outside the home network.
Nothing else on the router may lead to it: no port forward, no "exposed host", for
IPv4 **and** IPv6. The line is dual-stack, so the host has a globally routable IPv6
address, and the router's firewall is the boundary for that address as much as for
IPv4.

**Checked on 2026-10-18, after a finding.** The router check that #301 required
before this section could be written found two active port forwards for TCP 80 to
the production host, one over IPv4 and one over IPv6. They most likely dated from
the first week, before the VPN was the way in. Both are disabled. Afterwards port 80
did not answer from outside the home network over either protocol, while the LAN
still got the Basic Auth prompt. Whether anyone reached the stack while the forwards
existed cannot be told from the logs: every request in the nginx access log carries
Docker Desktop's internal gateway address, not the client's. That gap belongs to
[#303](https://github.com/liga-auguste/planning-hub/issues/303).

**What is assumed of the LAN.** `docker-compose.yml` publishes port 80 as `"80:80"`,
which binds every interface of the host. Any device on the home network therefore
reaches the login prompt. Basic Auth crosses the LAN base64-encoded, which is
readable by anyone who can see the traffic, not encrypted. The LAN is trusted on
that basis.

**Why `check --deploy` keeps warning here.** W004 (no HSTS), W008 (no HTTPS
redirect), W012 and W016 (session and CSRF cookies not marked `Secure`) all describe
a stack without TLS, which this one is. Silencing them would make the check claim
something untrue about the deployment. The demo, which terminates TLS, reports none
of them.

**What would change all of this.** Any access that does not go through the VPN,
whether a port forward, a tunnel or a reverse proxy on a public host, means HTTPS
comes first: a certificate, `HTTPS_ONLY` back at its default, and these four
warnings gone. Until then the boundary is the router, and the router check above
is what has to be repeated whenever its configuration changes.

## Not covered here

- Favicon rendering, the social preview card, and the healthcheck's
  `docker compose ps` status all need a manual look after deploy — none of them are
  meaningfully unit-testable.
- `.htpasswd` and `CSRF_TRUSTED_ORIGINS` are tracked in #24, not here.
