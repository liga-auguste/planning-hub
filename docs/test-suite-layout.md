# Test suite layout

Implements [#201](https://github.com/liga-auguste/planning-hub/issues/201).

`projects/tests.py` was one module of 11,302 lines holding 235 classes. The size was
never the problem — 2.4x the application code is a good ratio for a suite that is the
place delegated work gets checked. Being *one file* was: every session had to navigate
the whole thing before writing a line, two branches in flight collided in it even when
their subjects were unrelated, and picking the right class out of 235 was guesswork, so
near-duplicates accumulated.

It is now a package split by subject. Nothing else changed: no test body was touched, no
test was added, renamed or removed.

## Which subject lives where

| Module | Classes | Tests | Subject |
|---|---:|---:|---|
| `base.py` | 3 | 0 | Shared fixtures. Not collected — see below |
| `test_config.py` | 22 | 52 | Deployment, settings, environment, error pages, health check |
| `test_design.py` | 47 | 186 | The visual language: tokens, palette, dark theme, layout, what may appear on a page at all |
| `test_sidebar.py` | 25 | 101 | Nav, project list, progress rings, behaviour across viewports and views |
| `test_planner.py` | 33 | 104 | The four-step planner flow and the plan-generating calls behind it |
| `test_dashboard.py` | 23 | 55 | The dashboard read path: what renders, in which column, from which cache |
| `test_dashboard_writes.py` | 36 | 196 | Toggle and reschedule: what they persist, answer and leave in the cache |
| `test_week_view.py` | 14 | 78 | Heute / Diese Woche, the day columns, and the date helpers behind them |
| `test_timelapse.py` | 18 | 91 | Zeitreise: generated moments, the simulated date, the preloader |
| `test_my_plan.py` | 5 | 8 | `/mein-plan/` |
| `test_landing.py` | 2 | 7 | The landing page: what it renders, and where it sends a visitor |
| `test_summary.py` | 16 | 65 | The AI weekly summary: prompt, parsing, resolution, caches |
| `test_closeout.py` | 10 | 51 | Wochenabschluss: the ritual, its two backends, its summary |
| `test_notion.py` | 15 | 53 | `notion.py` directly, against a mocked API |
| `test_rules.py` | 7 | 51 | Planning rules: the page, both backends, seeding, the backfill migrations |
| `test_naming.py` | 13 | 48 | Display names and date formatting — what something is *called* on screen |
| **total** | **289** | **1146** | |

Two groups from the issue's suggested list are deliberately absent. There is no
legal-pages module: `/impressum/` and `/datenschutz/` are never the subject, only ever a
page a footer, sidebar or design test renders. And demo mode is a *mode*, not a subject —
166 classes inherit `DemoModeTestCase`, so it cuts across every module rather than
forming one. Four subjects the list did not name got their own module instead
(`test_config`, `test_design`, `test_summary`, `test_naming`); folding them into the
nearest neighbour would have made `test_dashboard.py` the new dumping ground.

The rule for placing a new test: it goes where its *subject* is, not where the code it
calls lives. A test that asserts a date renders as "12. September" through the dashboard
belongs in `test_naming.py`, because the dashboard is only how it got there.

## Why `base.py` is not called `test_base.py`

unittest discovers files matching `test*.py`. `base.py` does not match, so it is imported
but never collected — which is the only reason its three classes can be imported into
fifteen modules safely:

- `AiStubMixin` (with `AI_STUBS`) — not a `TestCase` at all
- `DemoModeTestCase` — a `TestCase` with no test method of its own
- `PlannerStepsMixin` — not a `TestCase` at all

A *concrete* test class could not be shared this way. unittest collects every `TestCase`
subclass it finds in a module's namespace, so an imported one would be counted once per
importing module, and the same test would run fifteen times.

`base.py` holds a fixture only when more than one module uses it. Everything used by a
single module — `_sidebar_group`, `_wcag_contrast`, `_cached_task`, `_fake_task_page` —
stays next to the class that uses it. A shared module is for what is actually shared.

Two classes in one module share fixtures the same way, in that module — `MomentFixtureMixin`
in `test_timelapse.py` holds the moment fixtures `NoToggleDuringAMomentTest` and
`AMomentSaysWhatItLocksTest` both need. A mixin rather than a shared base class, for the
same reason: it is not a `TestCase`, so unittest collects it nowhere, and the counts above
count it as a class with no tests of its own.

## Why `__init__.py` is not optional

Without `projects/tests/__init__.py`, unittest skips the directory silently. Not an
error, not a warning: `Ran 0 tests`, `OK`, exit code 0, CI green, suite dead. That is the
one way this change could have failed without anyone noticing, so the CI step no longer
trusts the exit code alone:

```yaml
set -o pipefail   # without it the pipeline reports tee's status and a failing run passes
python manage.py test projects --noinput 2>&1 | tee test-output.txt
grep -qE '^Ran [1-9][0-9]* tests' test-output.txt \
  || { echo '::error::the run collected no tests'; exit 1; }
```

Verified by deleting `__init__.py` and running it: `Ran 0 tests in 0.000s` followed by
`the run collected no tests`, exit 1.

## How the move itself was verified

A pure move admits no new test, so the check is not a test but a comparison —
`tools/dump_test_ids.py` dumps every collected test as `ClassName.method_name`, sorted,
deliberately *without* the module path, since the module path is the one thing this
change is allowed to alter:

```bash
python manage.py shell -v 0 -c "$(cat tools/dump_test_ids.py)" | LC_ALL=C sort > ids.txt
```

Captured on `main` before anything moved, captured again afterwards, `diff` empty at 868
lines. This is a stricter check than the count: one class collected twice and one lost
cancel out in a total but not in a list. Git cannot help here — it does not detect a
1 → 15 split as a rename, so the git diff is unreadably long by construction.

The move was additionally checked symbol by symbol: all 255 top-level definitions exist
in the package, byte-for-byte identical to their source in `tests.py`, decorators
included. Two section-banner comments (`# --- Unit tests for the logic that is not a
view ---`, `# --- #29: fail at startup, not at first request ---`) were dropped — they
described a structure the file no longer has.

## What is *not* part of this change

No test was rewritten, deduplicated or added. Import blocks were split per module — the
only edit outside a test body, and one the alternative makes mandatory: a module carrying
the full original import list fails CI on F401.

Near-duplicate classes noticed during the move are reported on the issue rather than
merged here. A move that also changes behaviour cannot be reviewed by comparing lists.
