# Design Tokens

Implements [Issue #11](https://github.com/liga-auguste/planning-hub/issues/11).

## Context

Before this, every color in the app was a hex literal repeated across 12 templates —
three different reds meant "overdue," three different oranges meant "urgent," and
changing one color meant search-and-replace across every file. A `:root` block of CSS
custom properties in both base templates (`base_public.html`, `base_dashboard.html`)
now names every color once; `{% block extra_css %}` sits inside the same `<style>`
element in both, so the tokens apply to every page automatically.

The token values follow [Linear](https://linear.app)'s palette rather than the app's
previous ad hoc grays — a deliberate visual refresh, not a mechanical extraction, since
[#12](https://github.com/liga-auguste/planning-hub/issues/12) needs the same palette
for its dark theme.

## Tokens

```css
:root {
    color-scheme: light;
    --color-bg-primary: #fff;
    --color-bg-secondary: #f9f8f9;
    --color-bg-tertiary: #f4f2f4;
    --color-border-primary: #e9e8ea;
    --color-border-secondary: #e4e2e4;
    --color-text-primary: #282a30;
    --color-text-secondary: #3c4149;
    --color-text-tertiary: #6f6e77;
    --color-text-quaternary: #86848d;
    --color-accent: #7070ff;
    --color-accent-hover: #8989f0;
    --color-accent-tint: #f1f1ff;
    --color-overdue: #ef4444;
    --color-today: #b88402;
    --color-done: #46a015;
    --color-overdue-tint: #fef2f2;
    --shadow-low: 0px 1px 4px -1px #00000017;
    --shadow-medium: 0px 3px 12px #00000017;
}
```

| Token | Use |
|---|---|
| `--color-bg-primary/secondary/tertiary` | Page, card, and hover-state backgrounds, darkest to lightest tint. `--color-bg-tertiary` is also the progress-track token both bars share (#212): Mein Plan's track moved here off `--color-border-primary`, against which the accent fill reached only 2.95:1 in dark mode |
| `--color-border-primary/secondary` | Hairline borders and dividers |
| `--color-text-primary` → `--color-text-quaternary` | Text and icon color, strongest to most muted |
| `--color-accent*` | The brand colour, and a **surface colour only**. Inside the product it paints two things, neither of which is a task state (#212): the active sidebar item (`--color-accent-tint`, the quiet coloured surface the token was declared for) and both progress bars (`.progress-fill` on the dashboard, `.progress-bar-fill` on Mein Plan — the same element under two class names, which is why they move together). Plus the older hover borders and `.week-nav-today`. It never colours text at 11–13px: WCAG 1.4.3 wants 4.5:1 there and the accent reaches 3.64:1 light / 4.34:1 dark on the page, so `.ai-project-link` keeps `--color-text-tertiary` and its `›` marker. As a fill, stroke or outline the floor is 3:1 for non-text UI, which it clears: 3.46:1 / 3.14:1 on the progress track, 3.85:1 / 3.63:1 on the sidebar card — the second pair now covering the focus outline, since #212's third placement is gone. The sidebar progress rings were the third: a brand-coloured stroke is read as a status the moment it sits beside the overdue red, whatever the stylesheet means by it, so `.progress-ring-fill` is back on `--color-text-quaternary` with `.overdue` still the only override. The constraint did not move — a surface that cannot be told apart from a status is not one of the accent's surfaces. `SignalColorContrastTest` computes the fill-and-stroke numbers. The text side is held by the rule instead of by the measurement — asserting that the accent *fails* 4.5:1 would make a later darkening of the brand colour, which is an improvement, fail the suite as a regression — so `BrandAccentPlacementTest` pins `.task-project.ai-project-link`'s own token and sweeps the rendered pages twice: for a `.dot`/`.task-due`/`.task-date`/`.kanban-card` rule serving the accent, and for an `.ai-project-link` one |
| `--color-overdue` / `--color-today` / `--color-done` | The three signal colors since #211: overdue red, due-today amber, done green — and green is **this week's** green. A task renders it only while its `completed_date` falls in the current ISO week (`done_this_week`, set in `_annotate_tasks` from `is_same_iso_week`); after that the dot returns to the neutral gray while the strike-through and the dimming stay for good. The unit is the week because the progress bar above the dots already reads "Diese Woche", so the green dots are exactly the tasks filling it — two displays, one statement, and no rolling-seven-days number to defend. A done task with no `completed_date` (checked off before "Erledigt am" existed, or checked off in Notion's own UI) renders gray, which is what keeps the whole back catalogue from being permanently green. Uneven dwell is the accepted price: Monday buys seven days of green, Sunday one. The demo's example projects carry one completion anchored to today and one to yesterday rather than to their own due dates: every other offset there is a week or more old, and under this rule a plausible-looking `d(-5)` renders gray from Monday to Friday, which left the public demo showing nine completed tasks and no green at all — `DemoDataCarriesThisWeeksGreenTest` walks all seven weekdays so the next edit to those offsets finds out here. `urgent` (due later in the same calendar week), `ok` and `undated` keep their classification in data and markup but render the neutral `--color-text-quaternary` — that restraint is the point, not an omission. Amber reaches the dot and the date label (`.task-due.today`, `.task-date.today`); the Kanban card accent stays out, and the sidebar progress rings stay out of the signal set because a ring shows a project's *total* completion while a dot shows a stage, so they render the same neutral gray with `.overdue` as their one override (#212 gave them the brand accent for a while; see the `--color-accent*` row for why that came back out). Both ring strokes clear the 3:1 floor on both surfaces a ring sits on — the sidebar card and, for an active project, #212's tinted item, where the gray reaches 3.29:1 light, the tightest pair in the palette; `SignalColorContrastTest` computes that pair separately from the dot's, because a dot never reaches the sidebar. Values are computed, not picked by eye: the amber hue came from a full hue-circle sweep in 8° steps, taking the tone whose *smallest* ΔE2000 distance to the colors already in play is largest (winner at 80° — ΔE 35.3 to overdue red, 32.5 to done green, 31.8 to the neutral gray, 61.0 to the accent). Green was only rotated toward yellow at unchanged lightness and chroma, then darkened. Every signal clears WCAG's 3:1 floor for non-text UI against *both* dot surfaces in both themes — the card (`--color-bg-primary`) on Mein Plan and the landing mockup, and the page itself (`--color-bg-secondary`) on the dashboard, whose `.main` declares no background of its own. `SignalColorContrastTest` computes that rather than trusting it. History: #160 gave due-today its own amber, #170 moved urgent to mustard after a ΔE analysis; #173 retired both warm stages because balancing three warm tones in a narrow band (amber sat ~ΔE 8 from the urgent orange) cost more than it bought, and explicitly reserved a later warm stage as additive; #211 took exactly one of them back, which works because only two warm tones now exist and nothing lives between hue 25° and 80° |
| `--color-overdue-tint` | Light background for the stale-data and error notices |
| `--shadow-low` / `--shadow-medium` | The two elevation levels — hover/active shadows, plus `--shadow-medium` for the sidebar's static elevation as a floating tile (#96) |

`--color-overdue-tint` extends the set #11 originally proposed — the stale-data
notice needed a light background for its status color, which the base 17 tokens
didn't name.

## Layout properties

Not colours, but the same mechanism and the same reason: one name for a number several
rules depend on. All three live on `:root` in `dashboard.css` (#236, #296).

```css
:root {
    --sidebar-width: var(--sidebar-width-stored, 260px);
    --sidebar-gap: 24px;
    --reading-width: 920px;
}
:root.sidebar-collapsed { --sidebar-width: 48px; }
```

| Property | Use |
|---|---|
| `--sidebar-width` | The sidebar's own width, and the basis of every rule that has to keep clear of it |
| `--sidebar-gap` | The fixed 24px between the floating sidebar and the content (#96): 12px viewport inset + 12px breathing room |
| `--reading-width` | `.main`'s cap, which the Heute view lifts for the week board and puts back on everything else in it, and the footer's (#296) |

Two rules read the sidebar properties, and before #236 both carried the numbers by hand —
260px and 48px in `dashboard.css`, 284px and 72px again for `.main`'s `margin-left`:

- `.sidebar { width: var(--sidebar-width) }`
- `.main { margin-left: calc(var(--sidebar-width) + var(--sidebar-gap)) }`

The day columns in `dashboard.html` were a third reader until #296: their width was
`100vw` minus the sidebar, which jumped while `.main`'s `margin-left` slid and hung the
board past the window mid-collapse. The board now stays inside `.main`, which drops its
`--reading-width` cap in the Heute view, so it follows the sidebar through layout alone.

### Why the stored width is a second property

`--sidebar-width-stored` is written by `_sidebar_preload.html`, a blocking script in
`<head>` that reads the drag width out of `localStorage` before the first paint. It writes
it **inline on `<html>`**, and an inline style beats any stylesheet rule.

That is precisely why it must not write `--sidebar-width` itself: the inline value would
beat `:root.sidebar-collapsed { --sidebar-width: 48px }`, and the collapsed rail would
never apply after a drag. That fight is [#137](https://github.com/liga-auguste/planning-hub/issues/137),
which used to be answered by clearing the inline styles by hand on every collapse path.
Against a *different* property there is nothing to clear: the stored width only ever acts
as the default's fallback, and the collapsed rule overrides the property that is actually
consumed. `clearInlineWidth()` and `applySavedWidth()` are gone rather than honoured in
one more place.

The state itself is a class on `:root` for the same before-the-first-paint reason: in
`<head>`, neither `.sidebar` nor `.main` exists yet. Every rule that used to read
`.sidebar.collapsed` or `.main.sidebar-collapsed` now reads `:root.sidebar-collapsed`,
including the ones inside the mobile block — converting one half of a pair and not the
other would change its specificity and silently kill the override that cascade order,
not specificity, was deciding.

## Where a component rule lives

Not a token question, but the one the tokens' own history answers: `base.css` is loaded
by **both** base templates, `public.css` only by `base_public.html` and `dashboard.css`
only by `base_dashboard.html`. So a rule for a component that the public pages render as
well belongs in `base.css`; one only the app needs belongs in `dashboard.css`.

[#289](https://github.com/liga-auguste/planning-hub/issues/289) split the task status dot
along exactly that line, and it is the clearest example of the rule because the dot sits
on both sides of it:

| Half | File | Why |
|---|---|---|
| Shape, size, `border`, the four state colours, the `forced-colors` block | `base.css` | `landing.html`'s mock renders the dot too, and `base_public.html` never loads `dashboard.css` |
| `padding`/`cursor` reset, `:hover`, `:focus-visible`, `margin-right` | `dashboard.css` | Only the app's dots are `<button>`s, and only its rows want the margin — the landing mock spaces its rows with a flex `gap` |

The `border` sits on the shape side for a reason worth generalising: a later sheet wins
on source order at equal specificity, so a property a `forced-colors` block in `base.css`
depends on cannot also be reset in `dashboard.css`. `button.dot` carried `border: none`
until [#289](https://github.com/liga-auguste/planning-hub/issues/289)'s forced-colors
point made the border the one thing that still draws the ring in high contrast; it moved
to `.dot`, where an author declaration strips the UA button border just as well. **If a
rule in the shared sheet is load-bearing, the reset for it belongs there too.**

Before that, the dot's full rule was written three times — once per template — which is
the duplication [#195](https://github.com/liga-auguste/planning-hub/issues/195) set out
to end and [#266](https://github.com/liga-auguste/planning-hub/issues/266) ended for
`.task-due` by the same move. Two things make the merge worth doing deliberately rather
than opportunistically:

- **`{% block extra_css %}` renders after the linked stylesheets.** A copy left in one
  template keeps winning the cascade, so moving a rule only takes effect once *every*
  copy is gone. `SignalDotColorTest` asserts that none of the three templates defines a
  `.dot` rule again.
- **The shared half reaches surfaces that do not use all of it.** The landing mock
  renders no completed task, so `.dot.done` is dead weight there. That is the accepted
  price of one rule — a separate sheet for the one state the marketing page skips would
  be the same duplication under another name.

The surface-specific overrides stay with their surface (`.ai-card span.dot`,
`.day-task-card .dot`, `.summary-box .dot`), with one exception on the same reasoning as
`.task-due`'s: `.task-row .dot` lives in `dashboard.css`, because `.task-row` is spelled
by the dashboard and by `/mein-plan/` alike and would otherwise be typed out twice.

## Page skeleton

[#298](https://github.com/liga-auguste/planning-hub/issues/298). Colours had rules;
the structure of a page had none. Every page defined its own title in its own `<style>`
block — six definitions of one role, 20 to 28px, weight 600 or 700, margins from 4 to
8px — so a new page copied whichever page it started from. The skeleton is now one rule
per part in `base.css`, which both base templates load:

```css
.page-header   { margin-bottom: 28px; }
.page-title    { font-size: 24px; font-weight: 600; line-height: 1.2;
                 letter-spacing: -0.02em; text-wrap: balance; margin: 0; }
.page-subtitle { font-size: 13px; line-height: 1.5; color: var(--color-text-tertiary); margin: 4px 0 0; }
.section-label { font-size: 11px; font-weight: 600; line-height: 1.5; color: var(--color-text-tertiary);
                 text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 10px; }
.list-row      { display: flex; align-items: center; gap: 12px; padding: 11px 0;
                 border-bottom: 1px solid var(--color-border-primary); }
.list-row:last-child { border-bottom: none; }
```

Every property Bootstrap's Reboot sets on `h1` and `p` is stated, so the classes look
the same on any element.

| Part | What it is for | Used by |
|---|---|---|
| Page header | `<div class="page-header">` around an `<h1 class="page-title">` and an optional `<p class="page-subtitle">` — the page's name and the line under it: a date (dashboard), a meta line (`/mein-plan/`), a week (close-out, review) or an explanatory sentence (stats, rules, planner, error pages) | Dashboard (both views, via `_page_heading.html`), `/mein-plan/`, Woche abschließen, Wochenrückblick, Statistik, Planungsregeln, the four planner titles, Impressum, Datenschutz, 404, 500 |
| Section label | The small uppercase heading above a block | None yet — #92 is the first adopter |
| List row | The row a list of tasks or rules is built from: height, padding, divider. `flex-wrap` is not part of it; it stays with the surfaces that need it | None yet — #92 is the first adopter |

Why these values:

- **24px, weight 600.** 24px was already the size on `/mein-plan/`, the close-out and
  the review, and sits a clear step above #288's 16px section heading. 600 is what every
  title except the dashboard's used.
- **The planner's headline treatment is the app's.** #72 gave the planner title the
  landing headline's tracking and balanced wrapping. "No page redefines the title" rules
  out a planner exception, so the shared rule took the treatment and the planner gave up
  its 28px. `text-wrap: balance` needs Safari 17.5; older versions wrap normally.
- **Subtitle and label are `--color-text-tertiary`.** Quaternary, which nearly every copy
  used, misses WCAG's 4.5:1 for text this size on three of the four surfaces it sits on;
  tertiary clears all four:

  |  | light page | light card | dark card | dark page |
  |---|---|---|---|---|
  | tertiary | 4.75 | 5.03 | 6.49 | 7.76 |
  | quaternary | 3.48 | 3.68 | 3.82 | 4.57 |

- **4px title to subtitle, 28px header to content** — the most common value of each. The
  4px sits on the subtitle, so a title without one carries no stray margin; inside the
  dashboard's wrapping heading row, a margin on the title would leave the date line flush
  under the project name once the row wraps at phone width.
- **Section label and list row** take the values most live copies already render:
  `.kanban-col-header`/`.timelapse-label` for the label, `.task-row` for the row. They are
  defined ahead of their adopters, so the rules describe something that renders today.

Decisions that look like oversights:

- **The wrapper is a `<div>`, not a `<header>`.** Outside `<main>`, a `<header>` is
  exposed as the page's `banner` landmark.
- **A page may pad `.page-header`; it may not give it a margin.** The 44px launcher
  clearance stays per page, as `MobileLauncherClearanceTest` requires — only where the
  header collides with the fixed ☰ at rest. On `/mein-plan/` the header is not at the
  top, and a shared reservation would shorten its title for nothing.
- **The landing headline stays outside.** It is the 42px poster cut #72 kept apart, not
  a page title; `landing.html` is the one template allowed a bare `h1` rule.
- **`.ai-card h1:not(.page-title)`.** The dashboard title sits inside `.ai-card`, where
  the summary's markdown headings are styled, and at (0,1,1) that rule beat the shared
  (0,1,0) title down to 16px until the `:not()` excluded it.

`PageSkeletonTest` holds all of this: the rules carry these values exactly once; no
template defines `.page-title`, `.page-subtitle`, `.section-label` or `.list-row`, styles
a bare `h1`, or puts a margin on `.page-header`; every page template renders its title
through the rule; every rendered page has exactly one; and the subtitle and label grey is
computed against both surfaces in both themes.

## Rule

**No new hex literals in templates.** If a color isn't one of the tokens above, either
reuse the closest existing token or add a new one here — don't reach for a raw hex
value in a template or a `style="..."` attribute.

**No page template defines its own title, subtitle, section label or list row.** Use
the page skeleton above; a page that needs something different changes the shared rule
and this document, not its own `<style>` block.
