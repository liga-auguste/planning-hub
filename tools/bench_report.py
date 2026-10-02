"""Turn two bench_page_loads.py runs into the tables in docs/async-ai-summary.md.

    python tools/bench_report.py <before.json> <after.json> [<before.log> <after.log>]

The two optional logs are the runserver output; the `claude_call` lines in
them (#31) are reported as the control: if the model's own durations are
indistinguishable between the two revisions, the difference in page time is
about where the call sits and nothing else.
"""

import json
import re
import sys


def percentile(values, p):
    """Nearest-rank, no interpolation. At n = 20 an interpolated p95 would
    invent a value between two real samples and read more precise than the
    data is."""
    ordered = sorted(values)
    rank = max(1, -(-len(ordered) * p // 100))
    return ordered[int(rank) - 1]


def summarise(values):
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "min": min(values),
        "max": max(values),
    }


def seconds(value):
    return f"{value * 1000:.1f} ms" if value < 0.1 else f"{value:.2f} s"


def load(path):
    with open(path) as handle:
        return json.load(handle)


def read(path):
    with open(path) as handle:
        return handle.read()


runs = {"before": load(sys.argv[1]), "after": load(sys.argv[2])}

for scenario, metrics in (
    ("catalog", ("page", "fragment")),
    ("planner", ("create", "dashboard", "visible", "fragment", "moments")),
):
    print(f"\n### {scenario}")
    print(
        f"{'metric':<12}{'side':<8}{'n':>4}{'p50':>12}{'p95':>12}{'min':>12}{'max':>12}"
    )
    for metric in metrics:
        for side in ("before", "after"):
            values = [s[metric] for s in runs[side][scenario] if metric in s]
            if not values:
                continue
            row = summarise(values)
            print(
                f"{metric:<12}{side:<8}{row['n']:>4}"
                f"{seconds(row['p50']):>12}{seconds(row['p95']):>12}"
                f"{seconds(row['min']):>12}{seconds(row['max']):>12}"
            )

if len(sys.argv) > 4:
    print("\n### claude_call, the control")
    for side, path in (("before", sys.argv[3]), ("after", sys.argv[4])):
        calls = {}
        for call, model, ms, tokens in re.findall(
            r"call=(\S+) model=(\S+) duration_ms=(\d+) input_tokens=(\d+)",
            read(path),
        ):
            calls.setdefault((call, model, tokens), []).append(int(ms))
        for (call, model, tokens), durations in sorted(calls.items()):
            row = summarise(durations)
            print(
                f"{side:<8}{call:<28}{model:<28}in={tokens:<6}"
                f"n={row['n']:<4}p50={row['p50']:>6}ms p95={row['p95']:>6}ms"
            )
