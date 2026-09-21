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
| `--color-accent*` | The brand colour, and a **surface colour only**. Inside the product it paints three things, none of which is a task state (#212): the sidebar progress rings (`.progress-ring-fill`, with `.overdue` red as the only override), the active sidebar item (`--color-accent-tint`, the quiet coloured surface the token was declared for), and both progress bars (`.progress-fill` on the dashboard, `.progress-bar-fill` on Mein Plan — the same element under two class names, which is why they move together). Plus the older hover borders and `.week-nav-today`. It never colours text at 11–13px: WCAG 1.4.3 wants 4.5:1 there and the accent reaches 3.64:1 light / 4.34:1 dark on the page, so `.ai-project-link` keeps `--color-text-tertiary` and its `›` marker. As a fill or stroke the floor is 3:1 for non-text UI, which it clears: 3.85:1 / 3.63:1 on the sidebar card, 3.46:1 / 3.14:1 on the progress track. `SignalColorContrastTest` computes the fill-and-stroke numbers. The text side is held by the rule instead of by the measurement — asserting that the accent *fails* 4.5:1 would make a later darkening of the brand colour, which is an improvement, fail the suite as a regression — so `BrandAccentPlacementTest` pins `.task-project.ai-project-link`'s own token and sweeps the rendered pages twice: for a `.dot`/`.task-due`/`.task-date`/`.kanban-card` rule serving the accent, and for an `.ai-project-link` one |
| `--color-overdue` / `--color-today` / `--color-done` | The three signal colors since #211: overdue red, due-today amber, done green — and green is **this week's** green. A task renders it only while its `completed_date` falls in the current ISO week (`done_this_week`, set in `_annotate_tasks` from `is_same_iso_week`); after that the dot returns to the neutral gray while the strike-through and the dimming stay for good. The unit is the week because the progress bar above the dots already reads "Diese Woche", so the green dots are exactly the tasks filling it — two displays, one statement, and no rolling-seven-days number to defend. A done task with no `completed_date` (checked off before "Erledigt am" existed, or checked off in Notion's own UI) renders gray, which is what keeps the whole back catalogue from being permanently green. Uneven dwell is the accepted price: Monday buys seven days of green, Sunday one. The demo's example projects carry one completion anchored to today and one to yesterday rather than to their own due dates: every other offset there is a week or more old, and under this rule a plausible-looking `d(-5)` renders gray from Monday to Friday, which left the public demo showing nine completed tasks and no green at all — `DemoDataCarriesThisWeeksGreenTest` walks all seven weekdays so the next edit to those offsets finds out here. `urgent` (due later in the same calendar week), `ok` and `undated` keep their classification in data and markup but render the neutral `--color-text-quaternary` — that restraint is the point, not an omission. Amber reaches the dot and the date label (`.task-due.today`, `.task-date.today`); the Kanban card accent stays out, and the sidebar progress rings stay out of the signal set because a ring shows a project's *total* completion while a dot shows a stage — #212 gave them the brand accent instead, which is not a task state. Values are computed, not picked by eye: the amber hue came from a full hue-circle sweep in 8° steps, taking the tone whose *smallest* ΔE2000 distance to the colors already in play is largest (winner at 80° — ΔE 35.3 to overdue red, 32.5 to done green, 31.8 to the neutral gray, 61.0 to the accent). Green was only rotated toward yellow at unchanged lightness and chroma, then darkened. Every signal clears WCAG's 3:1 floor for non-text UI against *both* dot surfaces in both themes — the card (`--color-bg-primary`) on Mein Plan and the landing mockup, and the page itself (`--color-bg-secondary`) on the dashboard, whose `.main` declares no background of its own. `SignalColorContrastTest` computes that rather than trusting it. History: #160 gave due-today its own amber, #170 moved urgent to mustard after a ΔE analysis; #173 retired both warm stages because balancing three warm tones in a narrow band (amber sat ~ΔE 8 from the urgent orange) cost more than it bought, and explicitly reserved a later warm stage as additive; #211 took exactly one of them back, which works because only two warm tones now exist and nothing lives between hue 25° and 80° |
| `--color-overdue-tint` | Light background for the stale-data and error notices |
| `--shadow-low` / `--shadow-medium` | The two elevation levels — hover/active shadows, plus `--shadow-medium` for the sidebar's static elevation as a floating tile (#96) |

`--color-overdue-tint` extends the set #11 originally proposed — the stale-data
notice needed a light background for its status color, which the base 17 tokens
didn't name.

## Layout properties

Not colours, but the same mechanism and the same reason: one name for a number several
rules depend on. Both live on `:root` in `dashboard.css` (#236).

```css
:root {
    --sidebar-width: var(--sidebar-width-stored, 260px);
    --sidebar-gap: 24px;
}
:root.sidebar-collapsed { --sidebar-width: 48px; }
```

| Property | Use |
|---|---|
| `--sidebar-width` | The sidebar's own width, and the basis of every rule that has to keep clear of it |
| `--sidebar-gap` | The fixed 24px between the floating sidebar and the content (#96): 12px viewport inset + 12px breathing room |

Three rules read them, and before #236 all three carried the numbers by hand — 260px and
48px in `dashboard.css`, 284px and 72px again for `.main`'s `margin-left`, and 284/72
a third time in `dashboard.html`'s day-column width:

- `.sidebar { width: var(--sidebar-width) }`
- `.main { margin-left: calc(var(--sidebar-width) + var(--sidebar-gap)) }`
- `.day-columns { width: calc(100vw - var(--sidebar-width) - var(--sidebar-gap) - 68px) }`
  (`dashboard.html`) — the easiest of the three to forget, and the most visible when it
  lags behind the other two

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

## Rule

**No new hex literals in templates.** If a color isn't one of the tokens above, either
reuse the closest existing token or add a new one here — don't reach for a raw hex
value in a template or a `style="..."` attribute.
