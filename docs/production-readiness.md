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

### og-image.png as its own static copy

`og:image` reuses the dashboard screenshot from #26, but as a dedicated copy under
`projects/static/projects/og-image.png` rather than a reference into `docs/`. The
app's static pipeline (`collectstatic`, content hashing) shouldn't depend on a
directory GitHub renders separately and that isn't guaranteed to exist in every
checkout shape.

## Not covered here

- Favicon rendering, the social preview card, and the healthcheck's
  `docker compose ps` status all need a manual look after deploy — none of them are
  meaningfully unit-testable.
- `.htpasswd` and `CSRF_TRUSTED_ORIGINS` are tracked in #24, not here.
