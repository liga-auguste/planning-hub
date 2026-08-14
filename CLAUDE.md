# Planning Hub — project conventions

Django app that turns a plain-language project description into a task plan via the Claude API, writes it to Notion, and tracks it on a dashboard with AI-generated weekly summaries. The audience is German-speaking, so the product is German while the engineering around it is English.

## Language convention

**Anything that lands on GitHub is English. Anything only for the maintainer is German. Anything app users see is German.**

| German | English |
|---|---|
| UI templates and every user-visible string | Code identifiers (variables, functions, classes) |
| `choices` labels, `verbose_name`, `help_text` | Comments and docstrings |
| Claude prompts **and the output they produce** | Developer-facing output (`stdout.write`, `print`, `help=`) |
| Legal pages, cookie banner | Commit messages, branch names |
| Markdown export, seed *data* | GitHub issues, pull requests, PR comments |
| Local session notes, planning documents | `README.md`, `docs/*.md`, this file |

The dividing line is the audience, not the file type. `seed_rules.py` holds both: the command's `help=` ("Seed initial planner rules") and its `stdout.write` ("Rules already present, skipped.") are English because a developer reads them, while the rules it seeds ("Bei Konzertveranstaltungen GEMA-Meldung einplanen — nicht bei Gottesdiensten") are German because they go into the Claude prompt and are shown in the UI.

Claude prompts are German on purpose — they carry explicit instructions such as "Auf Deutsch, Du-Form", and their output is rendered straight into the interface. Their JSON *keys* stay English (`date`, `label`, `days_before`); only the values are German.

### Deliberate exceptions

These are German by decision. Do not "fix" them:

- **`kontext` / `KONTEXTE` / `TASK_KONTEXT`** — a domain term. The values ("Büro", "Planung", "Vor Ort") appear in prompts, come back from Claude, are stored in the database and shown in the UI. Renaming the identifier would split it from its own values for no gain.
- **`impressum()` / `datenschutz()`** — established legal terms, coupled to their URL paths and templates.
- **`MONTHS_DE`, `MONTHS_SHORT`, `WEEKDAYS_SHORT`** — English identifier, German values. Correct as is.
- **Notion property names** (`"Name der Veranstaltung"`, `"Wann?"`) — defined by the external database, not by us.
- **German URL paths** (`mein-plan/`, `regeln/`) — inconsistent with `dashboard/` and `stats/`, tracked in #15 rather than changed ad hoc.

### Hard constraint

`impressum.html`, `datenschutz.html` and the cookie banner must stay German. This is a legal requirement (GDPR / §5 TMG), not a stylistic choice.

## Emoji and symbols

**Pictographic emoji are not part of the design language — not in templates and not in Claude prompts.** Prompts count as UI here for the same reason the language convention treats them as user-facing: their output renders straight into the interface, and Claude echoes the register of its input. Where an icon is warranted, it is a Lucide SVG (ISC, see `README.md`), the way the planner tiles do it.

### Deliberate exception

- **Typographic symbols are part of the visual language.** The arrows (`←` `→` `↓`), `⚙` on the rules link, `⠿` as the drag handle, `×` on delete buttons. They render in the text colour and follow `color`, which pictographic emoji ignore. Do not "fix" them into icons.
