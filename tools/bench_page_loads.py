"""Time the three page loads #156 is about, n samples each.

    # one terminal, from a checkout of the revision being measured:
    DEMO_MODE=true python manage.py runserver 8765 --noreload

    # another:
    python tools/bench_page_loads.py <label> <n> <out.json> <that checkout's path>

Writes every raw sample to <out.json>; `tools/bench_report.py` turns two of
those into the percentile tables in docs/async-ai-summary.md.

Measure the two revisions from two `git worktree`s rather than by switching
branches: each needs its own SQLite file, and a `git checkout` in a tree with
uncommitted work is not a thing a benchmark should be doing.

Deliberately not a test. It calls the real API, costs real money and takes
minutes, and its numbers are a snapshot of one machine on one day. What the
suite guards is the structural claim instead — see PagesDoNotWaitOnClaudeTest.
"""

import json
import re
import subprocess
import sys
import time
from datetime import date, timedelta

import httpx

BASE = "http://127.0.0.1:8765"
# Discarded: the first requests pay import and connection costs the rest do not.
WARMUP = 2
TOKEN_RE = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')

LABEL, N, OUT, ROOT = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]


def clear_cache():
    """Every sample is a cold one — otherwise sample 2 onwards measures the
    cache and the thing under test never runs."""
    subprocess.run(
        [
            sys.executable,
            "manage.py",
            "shell",
            "-v",
            "0",
            "-c",
            "from django.core.cache import cache; cache.clear()",
        ],
        cwd=ROOT,
        env={"DEMO_MODE": "true", "PATH": "/usr/bin:/bin", "HOME": "/"},
        capture_output=True,
        check=False,
    )


def timed(client, method, path, **kw):
    """Wall clock around the whole request, connection setup included — the
    same thing curl reports as time_total, and closer to what a browser waits
    than a server-side timer would be."""
    started = time.perf_counter()
    response = client.request(method, BASE + path, **kw)
    return time.perf_counter() - started, response


def catalog_sample():
    """The uncached multi-project dashboard. Before #156 the Claude call is
    inside the page; after it, in the fragment the page then asks for."""
    clear_cache()
    with httpx.Client(follow_redirects=False, timeout=60) as client:
        page_seconds, page = timed(client, "GET", "/dashboard/?mode=multi")
        sample = {"page": page_seconds}
        if "data-summary-url" in page.text:
            token = TOKEN_RE.search(page.text).group(1)
            sample["fragment"], _ = timed(
                client,
                "POST",
                "/summary/?mode=multi",
                headers={"X-CSRFToken": token, "Referer": BASE + "/dashboard/"},
            )
        return sample


def planner_sample():
    """ "Plan speichern" through to a painted dashboard. `visible` is the sum
    of the two: what the visitor waits for before seeing anything at all."""
    clear_cache()
    event = (date.today() + timedelta(days=60)).isoformat()
    first = (date.today() + timedelta(days=10)).isoformat()
    second = (date.today() + timedelta(days=30)).isoformat()
    with httpx.Client(follow_redirects=False, timeout=60) as client:
        start = client.get(BASE + "/planner/?type=konzert")
        token = TOKEN_RE.search(start.text).group(1)
        create_seconds, _ = timed(
            client,
            "POST",
            "/planner/create/",
            headers={"Referer": BASE + "/planner/"},
            data={
                "csrfmiddlewaretoken": token,
                "description": "Adventskonzert",
                "project_name": "Adventskonzert",
                "event_date": event,
                "task_name": ["Programm festlegen", "Noten bestellen"],
                "task_date": [first, second],
                "task_kontext": ["Planung", "Planung"],
            },
        )
        dashboard_seconds, page = timed(client, "GET", "/dashboard/")
        sample = {
            "create": create_seconds,
            "dashboard": dashboard_seconds,
            "visible": create_seconds + dashboard_seconds,
        }
        if "data-summary-url" in page.text:
            token = TOKEN_RE.search(page.text).group(1)
            headers = {"X-CSRFToken": token, "Referer": BASE + "/dashboard/"}
            sample["fragment"], _ = timed(client, "POST", "/summary/", headers=headers)
            sample["moments"], _ = timed(
                client, "POST", "/timelapse/moments/", headers=headers
            )
        return sample


SCENARIOS = {"catalog": catalog_sample, "planner": planner_sample}

samples = {name: [] for name in SCENARIOS}
for name, take in SCENARIOS.items():
    for i in range(WARMUP + N):
        sample = take()
        if i >= WARMUP:
            samples[name].append(sample)
        print(f"{LABEL} {name} {i + 1}/{WARMUP + N} {sample}", flush=True)

with open(OUT, "w") as handle:
    json.dump(samples, handle, indent=1)
print("written", OUT)
