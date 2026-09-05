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
| `--color-bg-primary/secondary/tertiary` | Page, card, and hover-state backgrounds, darkest to lightest tint |
| `--color-border-primary/secondary` | Hairline borders and dividers |
| `--color-text-primary` → `--color-text-quaternary` | Text and icon color, strongest to most muted |
| `--color-accent*` | Interactive/focus accent (links, hover borders) |
| `--color-overdue` / `--color-today` / `--color-done` | The three signal colors since #211: overdue red, due-today amber, done green. `urgent` (due later in the same calendar week), `ok` and `undated` keep their classification in data and markup but render the neutral `--color-text-quaternary` — that restraint is the point, not an omission. Amber reaches the dot and the date label (`.task-due.today`, `.task-date.today`); the Kanban card accent and the sidebar progress rings stay out. Values are computed, not picked by eye: the amber hue came from a full hue-circle sweep in 8° steps, taking the tone whose *smallest* ΔE2000 distance to the colors already in play is largest (winner at 80° — ΔE 35.3 to overdue red, 32.5 to done green, 31.8 to the neutral gray, 61.0 to the accent). Green was only rotated toward yellow at unchanged lightness and chroma, then darkened. Every signal clears WCAG's 3:1 floor for non-text UI against *both* dot surfaces in both themes — the card (`--color-bg-primary`) on Mein Plan and the landing mockup, and the page itself (`--color-bg-secondary`) on the dashboard, whose `.main` declares no background of its own. `SignalColorContrastTest` computes that rather than trusting it. History: #160 gave due-today its own amber, #170 moved urgent to mustard after a ΔE analysis; #173 retired both warm stages because balancing three warm tones in a narrow band (amber sat ~ΔE 8 from the urgent orange) cost more than it bought, and explicitly reserved a later warm stage as additive; #211 took exactly one of them back, which works because only two warm tones now exist and nothing lives between hue 25° and 80° |
| `--color-overdue-tint` | Light background for the stale-data and error notices |
| `--shadow-low` / `--shadow-medium` | The two elevation levels — hover/active shadows, plus `--shadow-medium` for the sidebar's static elevation as a floating tile (#96) |

`--color-overdue-tint` extends the set #11 originally proposed — the stale-data
notice needed a light background for its status color, which the base 17 tokens
didn't name.

## Rule

**No new hex literals in templates.** If a color isn't one of the tokens above, either
reuse the closest existing token or add a new one here — don't reach for a raw hex
value in a template or a `style="..."` attribute.
