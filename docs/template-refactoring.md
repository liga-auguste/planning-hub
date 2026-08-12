# Template Inheritance Refactoring

Implements [Issue #1](https://github.com/liga-auguste/planning-hub/issues/1).

## Context

All 10 Django templates currently contain full HTML boilerplate (~60 lines each). Two layout
archetypes exist in practice but are not expressed as Django template inheritance. The
cookie-banner code is duplicated between `landing.html` and `dashboard.html`. The progress
tracker (`.ps-track`) is copy-pasted across three planner templates. This refactoring
consolidates all of that without any visual change.

**Branch:** `feature/template-base-layouts`

---

## New files

| File | Purpose |
|------|---------|
| `projects/templates/projects/base_public.html` | Base for 7 public pages |
| `projects/templates/projects/base_dashboard.html` | Base for 3 dashboard pages |
| `projects/templates/projects/_cookie_banner.html` | Extracted include partial |
| `projects/templates/projects/_progress.html` | Progress tracker with `active_step` param |

**Public templates** (extend `base_public.html`):
`landing.html`, `planner_start.html`, `planner_questions.html`, `planner_review.html`,
`planner_rules.html`, `datenschutz.html`, `impressum.html`

**Dashboard templates** (extend `base_dashboard.html`):
`dashboard.html`, `my_plan.html`, `stats.html`

---

## Doubts / things to watch

1. **`planner_questions` and `planner_review` have no GET handler** — both views return `None`
   on GET. Smoke tests for these must POST with mocked AI calls, or only assert file existence.

2. **`my_plan.html` and `stats.html` have no sidebar** — they override `{% block full_page %}`
   in `base_dashboard.html` to render a simple centered layout. The sidebar div must not appear
   on these pages.

3. **`planner_start.html` has two progress tracker states** in one template (`{% if show_tiles %}`):
   step 1 (tile selection) and step 2 (description). The child template handles this with a
   conditional include inside `{% block content %}`.

4. **Bootstrap added to 5 previously Bootstrap-free pages** (landing, planner_rules, datenschutz,
   impressum, stats). The existing `* { box-sizing: border-box; margin: 0; padding: 0; }` reset
   is equivalent to Bootstrap's normalize — no visual regression expected.

5. **The `dashboard` view calls AI functions** — dashboard and my_plan tests must mock
   `projects.views.generate_weekly_summary`.

---

## TDD: Tests first

All tests go in `projects/tests.py`. Four classes:

### `PublicPageSmokeTests`
- `@override_settings(DEMO_MODE=True)` at class level
- GET tests for: `/`, `/planner/`, `/planner/?type=konzert`, `/planner/regeln/`,
  `/impressum/`, `/datenschutz/`
- Each asserts: status 200, `assertTemplateUsed(base_public.html)`, wordmark text,
  cookie-banner div, footer links

### `DashboardPageSmokeTests`
- `@override_settings(DEMO_MODE=True)`, mock `generate_weekly_summary`
- Inject `demo_plan` session key for `/dashboard/` and `/mein-plan/`
- `/stats/` needs no session (returns empty stats from clean DB)
- Each asserts: status 200, `assertTemplateUsed(base_dashboard.html)`, cookie-banner div

### `PartialIncludeTests`
- `assertTemplateUsed(response, 'projects/_cookie_banner.html')` for a public and dashboard page
- `assertTemplateUsed(response, 'projects/_progress.html')` for both GET variants of `planner_start`

### `ProgressTrackerTests`
- `/planner/` (no `?type`): step 1 is `active`, steps 2–4 pending
- `/planner/?type=konzert`: step 1 is `done`, step 2 is `active`
- Asserted via `assertContains` on rendered HTML class order

All tests start **red** until implementation is complete.

---

## Block structure

### `base_public.html`

```html
{% load static %}
<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{% block title %}Planning Hub{% endblock %}</title>
    <link href="{% static 'projects/css/bootstrap.min.css' %}" rel="stylesheet">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #f7f7f8; color: #1a1a1a; font-family: -apple-system, ... }
        .wordmark { ... }
        .page-footer { ... }
        #cookie-banner { ... }      /* CSS for the partial */
        .ps-track, .ps-step, ...   /* Progress tracker CSS */
    </style>
    {% block extra_css %}{% endblock %}
</head>
<body>
{% block content %}{% endblock %}
{% include 'projects/_cookie_banner.html' %}
<script src="{% static 'projects/js/bootstrap.bundle.min.js' %}"></script>
{% block extra_js %}{% endblock %}
</body>
</html>
```

The top-bar (wordmark + progress tracker) lives inside the child's `{% block content %}`,
not in a dedicated base block, because landing, planner, and text pages all have different
top-of-page layouts.

### `base_dashboard.html`

```html
{% load static %}
<!DOCTYPE html>
<html lang="de">
<head>
    <!-- Bootstrap CSS + shared dashboard styles -->
    {% block extra_css %}{% endblock %}
</head>
<body>
{% block full_page %}
  <div class="sidebar" id="sidebar">
    <button class="sidebar-toggle">...</button>
    <div class="sidebar-resize"></div>
    <div class="sidebar-content">{% block sidebar_content %}{% endblock %}</div>
  </div>
  <div class="main" id="main">
    <div class="content-body">{% block content %}{% endblock %}</div>
  </div>
{% endblock %}
{% include 'projects/_cookie_banner.html' %}
<script src="{% static 'projects/js/bootstrap.bundle.min.js' %}"></script>
{% block extra_js %}{% endblock %}
</body>
</html>
```

`my_plan.html` and `stats.html` override `{% block full_page %}` with their own centered
layout. `dashboard.html` fills `{% block sidebar_content %}`, `{% block content %}`, and
`{% block extra_js %}` (sidebar JS + task/timelapse JS).

### `_progress.html`

```html
{# active_step: 1=Projekttyp 2=Beschreiben 3=Klärung 4=Review #}
<div class="ps-track">
    <div class="ps-step {% if active_step == 1 %}active{% elif active_step > 1 %}done{% endif %}">
        <div class="ps-dot"></div><span class="ps-label">Projekttyp</span>
    </div>
    <div class="ps-step {% if active_step == 2 %}active{% elif active_step > 2 %}done{% endif %}">
        <div class="ps-dot"></div><span class="ps-label">Beschreiben</span>
    </div>
    <div class="ps-step {% if active_step == 3 %}active{% elif active_step > 3 %}done{% endif %}">
        <div class="ps-dot"></div><span class="ps-label">Klärung</span>
    </div>
    <div class="ps-step {% if active_step == 4 %}active{% endif %}">
        <div class="ps-dot"></div><span class="ps-label">Review</span>
    </div>
</div>
```

### `_cookie_banner.html`

Canonical HTML from the `landing.html` version (cleaner than the dashboard inline-style variant).
The `#cookie-banner` CSS lives in the base templates' `<style>` block, not in this partial.

---

## Conversion order

1. Create feature branch
2. Write all tests — all red
3. Create the 4 new template files (empty shells)
4. Build `base_public.html` + convert `datenschutz.html`, `impressum.html` → first green tests
5. Convert `landing.html` (cookie-banner CSS moves to base; logo CSS stays in `extra_css`)
6. Convert `planner_rules.html` (Sortable.js CDN tag goes in `{% block extra_js %}`)
7. Convert `planner_start.html`, `planner_questions.html`, `planner_review.html`
   — replace inline `.ps-track` blocks with `{% include '_progress.html' with active_step=N %}`
8. Build `base_dashboard.html` + convert `stats.html`, `my_plan.html` (override `full_page`)
9. Convert `dashboard.html` (~1000 lines; base takes ~490 lines of boilerplate)
10. All tests green
11. Manual visual check of all 10 pages in browser

---

## Verification

```bash
python manage.py test projects

DEMO_MODE=true python manage.py runserver
# Check all 10 pages — must look identical to before
# / /planner/ /planner/?type=konzert /planner/regeln/
# /impressum/ /datenschutz/
# /dashboard/ /mein-plan/ /stats/
```

After refactoring: no child template contains `{% load static %}` or full HTML boilerplate.

---

## Note (2026-08-12, [#64](https://github.com/liga-auguste/planning-hub/issues/64))

Two details of the base-template skeletons above are now out of date. The skeletons are
left as written, because this document records the refactor as it was carried out.

- **The Bootstrap JS bundle is gone.** Both skeletons still show
  `<script src="{% static 'projects/js/bootstrap.bundle.min.js' %}"></script>`. Nothing
  initialised a Bootstrap component — zero `data-bs-*` attributes across all templates —
  so the 76 KB bundle was dropped when the CSS moved to 5.3.8. Only
  `projects/static/projects/css/bootstrap.min.css` is vendored now.
- **`base_dashboard.html` resets like `base_public.html`.** Point 4 of "Decisions" above
  reads the reset as equivalent to Bootstrap's normalize; in practice only the public base
  carried it and the dashboard base inherited Reboot's instead. Both now declare
  `* { box-sizing: border-box; margin: 0; padding: 0; }`.
