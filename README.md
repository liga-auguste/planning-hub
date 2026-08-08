# Planning Hub

An AI-powered project planning assistant. Describe an event or project in plain language, get clarifying questions from Claude, review and edit a generated task plan with realistic deadlines — then write it directly to Notion and track everything on a live dashboard with AI-generated weekly summaries.

> **Live demo:** [planninghub.ligaauguste.de](https://planninghub.ligaauguste.de) — no login required
>
> The interface and all AI-generated output are in German; the codebase and its documentation are in English. See [`CLAUDE.md`](CLAUDE.md) for the full convention.

---

## The problem it solves

Running multiple parallel projects means repeating the same planning process each time: coordinating people, booking services, managing communication, hitting deadlines. Each project type has its own rules and timelines.

The existing setup was a Notion view filtered by "this week" — useful, but it required knowing what to look for. Nothing surfaced what was actually urgent, what was missing, or how a new event compared to past ones.

---

## How it works

**Event planner** — Four-step flow:
1. Choose project type (concert, wedding, recruiting, custom…)
2. Describe the project in free text ("Outdoor concert, ~200 attendees, ticketed, external musicians")
3. Claude asks up to 4 clarifying questions — only what actually changes the task list
4. Review an editable task table with pre-filled dates, then save or write to Notion

**Dashboard** — Claude reads all active projects and open tasks, then generates a prioritised summary in plain language. Not a list of raw data, but an actual status read: what's urgent, what's on track, what needs attention today.

**Time-lapse simulation** — In demo mode, jump to any point in the project timeline and see how the dashboard would look: which tasks would be done, what the AI summary says, what's urgent. Claude picks 4 narrative moments (e.g. "6 weeks out", "dress rehearsal", "day after") per project.

---

## Architecture decisions

### RAG without a vector database

Historical events (49 completed projects since 2021) are loaded directly into Claude's context as structured text. No embeddings, no vector index. At this data volume, the full history fits in a single context window — simpler code, no infrastructure overhead, and Claude can reason across all past events at once rather than just the top-k nearest neighbours.

### Domain rules in the prompt, not in code

Every domain has planning rules that Claude doesn't know by default — legal requirements, vendor workflows, internal conventions. These are encoded explicitly in the prompt as editable `PlannerRule` objects (stored in the database, editable via admin UI) rather than as conditional logic. Planning heuristics stay readable, adjustable, and separate from application code.

### Notion as source of truth, Django for future AI memory

Notion already holds years of project history and is the daily working environment. Rather than migrating data, the app reads and writes Notion directly. Django's local database is reserved for data that shouldn't leave the machine — and for future AI memory (patterns learned from past projects). Deliberate separation of what goes where.

### Context derivation in code, not in Notion

Each task belongs to a workflow context (e.g. planning, admin, on-site). Instead of requiring manual tagging in Notion for every task, `derive_kontext()` in `ai.py` infers context from task names via a keyword mapping. The Notion database stays clean; the AI prompt groups tasks by context across all active projects.

---

## Features

- **AI weekly summary** — Claude response rendered as Markdown, with links to individual projects
- **Event planner** — free-text → clarifying questions → editable task table → Notion write
- **Time-lapse simulation** — jump to any point in the project timeline, see AI summary for that moment
- **Kanban view** — Open / Urgent / Done columns with progress bar
- **Task management** — check tasks done, reschedule due dates, "→ today" shortcut for overdue tasks
- **Urgency system** — overdue / urgent / on track per task and project, colour-coded in sidebar
- **Plan download** — export session plan as Markdown with AI-tool tips
- **Usage stats** — anonymous event tracking (plans generated / downloaded, by project type)
- **Editable planning rules** — drag-and-drop admin UI, toggle on/off, no code change needed
- **8h caching** — Notion API responses cached locally; manual sync button
- **DEMO_MODE** — runs on fixture data, no Notion credentials needed

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Django 6.0, Python 3.12 |
| Database | PostgreSQL 16 (prod) · SQLite (demo) |
| AI | Claude API — `claude-sonnet-4-6` (summaries) · `claude-haiku-4-5` (time-lapse) |
| Data source | Notion API (notion-client 2.2.1) |
| Frontend | Bootstrap 5 (local), vanilla JS |
| Web server | Nginx + Gunicorn |
| Deployment | Docker Compose, Hetzner VPS |

---

## Setup

### Local development

```bash
git clone https://github.com/liga-auguste/planning-hub
cd planning-hub
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env`:

```
ANTHROPIC_API_KEY=your_key
DEMO_MODE=true
SECRET_KEY=any-local-secret
```

A missing `ANTHROPIC_API_KEY` (or `NOTION_API_KEY` outside `DEMO_MODE`) fails
immediately with a clear message when the server starts, not on the first
request.

Run:

```bash
python manage.py migrate
python manage.py runserver
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000)

### Tests

```bash
python manage.py test projects
```

The Claude API is stubbed in `DemoModeTestCase`, so the suite makes no network calls and needs no API key:

```bash
env -u ANTHROPIC_API_KEY python manage.py test projects
```

### With Notion (full mode)

Add to `.env`:

```
NOTION_API_KEY=your_notion_key
DEMO_MODE=false
DB_HOST=localhost
DB_PASSWORD=your_db_password
```

In `projects/notion.py`, replace the two database IDs with your own:

```python
PROJECTS_DB = "your-projects-database-id"
TASKS_DB    = "your-tasks-database-id"
```

```bash
createdb planning_hub
python manage.py migrate
python manage.py seed_rules
python manage.py runserver
```

### Docker (demo)

```bash
cp .env.example .env  # fill in ANTHROPIC_API_KEY + SECRET_KEY
docker compose -f docker-compose.demo.yml up --build -d
```

### Docker (production)

```bash
cp .env.example .env  # fill in all keys including NOTION_API_KEY + DB_PASSWORD
docker compose up --build -d
```

---

## Project structure

```
projects/
  ai.py              # Claude API calls: weekly summary + planner
  planner.py         # RAG-based plan generation (questions + tasks)
  notion.py          # Notion API read/write
  demo_data.py       # Fixture data for DEMO_MODE
  views.py           # Dashboard, task toggle, time-lapse, stats
  planner_views.py   # 4-step planner flow
  models.py          # PlannerRule, DemoEvent
```

---

## License

MIT — built by [Liga Auguste](https://ligaauguste.de)
