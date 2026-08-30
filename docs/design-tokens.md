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
    --color-urgent: #ca8a04;
    --color-today: #b45309;
    --color-done: #22c55e;
    --color-overdue-tint: #fef2f2;
    --color-urgent-tint: #fefce8;
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
| `--color-overdue` / `--color-urgent` / `--color-today` / `--color-done` | The one status color per state — previously 3 reds and 3 oranges. `--color-today` (#160) is amber-700 light: amber-600 sat at ΔE2000 ≈ 8 from the then-orange urgent, too close to tell apart at 7 px dot size. `--color-urgent` (#170) is yellow-600 mustard light / yellow-400 dark: the retired orange sat at ΔE2000 19.7/23.5 from the overdue red, and the replacement must clear that distance against *both* neighbors (red and today) in each theme. Dark `--color-today` moved to amber-600 in the same change — every dark-capable yellow sits practically on top of amber-400, so today made room |
| `--color-overdue-tint` / `--color-urgent-tint` | Light backgrounds for status badges and notices |
| `--shadow-low` / `--shadow-medium` | The two elevation levels — hover/active shadows, plus `--shadow-medium` for the sidebar's static elevation as a floating tile (#96) |

`--color-overdue-tint` and `--color-urgent-tint` extend the set #11 originally
proposed — the kanban column counters and the stale-data notice needed a light
background for each status color, which the base 17 tokens didn't name.

## Rule

**No new hex literals in templates.** If a color isn't one of the tokens above, either
reuse the closest existing token or add a new one here — don't reach for a raw hex
value in a template or a `style="..."` attribute.
