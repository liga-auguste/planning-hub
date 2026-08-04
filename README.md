# Planning Hub

An AI-powered event planning assistant for church music work. Replaces scattered checklists and manual task tracking with a Claude-driven workflow: describe an event in plain language, get clarifying questions, review and edit a generated task plan, then write it to Notion with one click.

> **Try the live demo:** [link coming soon] — no login required, runs on fixture data

---

## The problem it solves

Running parallel church music events (concerts, services, recitals) means repeating the same planning process for each one: coordinating musicians, submitting GEMA copyright reports, briefing the graphic designer, arranging press coverage. Each event type has its own rules and timelines.

The existing setup was a Notion view filtered by "this week" — useful, but it required knowing what to look for. Nothing surfaced what was actually urgent, what was missing, or how a new event compared to past ones.

---

## How it works

**Weekly dashboard** — Claude reads all active projects and open tasks from Notion, then generates a prioritised summary in plain German. Not a list of raw data, but an actual status read: what's urgent, what's on track, what needs attention today.

**Event planner** — Three-step flow:
1. Describe the event in free text ("Orgelkonzert mit Solisten, ca. 120 Personen, Eintritt")
2. Claude asks up to 4 clarifying questions — only what actually changes the task list
3. Review an editable task table with pre-filled dates, then write project + tasks to Notion

---

## Architecture decisions

### RAG without a vector database

Historical events (49 completed concerts since 2021) are loaded directly into Claude's context as structured text. No embeddings, no vector index. At this data volume, the full history fits in a single context window, which means simpler code, no infrastructure overhead, and Claude can reason across all past events at once — not just the top-k nearest neighbours.

### Domain rules in the prompt, not in code

Church music planning has hard rules that Claude doesn't know by default:
- GEMA copyright reports: always for concerts, never for services — depends on event type, not repertoire
- Musician contracts + fees: always when external musicians are involved
- Poster design: standard print for concerts, home-printed for services

These are encoded explicitly in the prompt rather than as conditional logic. This keeps the planning heuristics readable, adjustable, and separate from application code.

### Notion as source of truth, Django for future AI memory

Notion already holds years of project history and is the daily working environment. Rather than migrating data to a new database, the app reads and writes Notion directly. Django's local database is reserved for data that shouldn't leave the local machine — sensitive fields like musician fees and names — and for future AI memory (patterns learned from past projects). Deliberate separation of what goes where.

### Context derivation in code, not in Notion

Each task belongs to a workflow context (Planning, Office, Graphic Designer, Communication, On Site). Instead of requiring manual tagging in Notion for every task, `derive_kontext()` in `ai.py` infers context from task names via a keyword mapping. This keeps the Notion database clean and means the AI prompt can group tasks by workflow context across all active projects.

---

## Features

- **AI weekly summary** — streaming Claude response, rendered as Markdown, with links to individual projects
- **Event planner** — free-text input → clarifying questions → editable task table → Notion write
- **Task management** — check tasks done, reschedule due dates (native date picker, "→ today" shortcut for overdue tasks), all synced to Notion
- **Urgency system** — overdue / urgent / on track status per task and project, colour-coded in sidebar
- **8h caching** — Notion API responses cached locally; manual sync button to force refresh
- **DEMO_MODE** — runs on fixture data, no Notion credentials needed (see below)

---

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Django 6.0, Python |
| Database | PostgreSQL 16 |
| AI | Claude API (claude-sonnet-4-6), Anthropic Python SDK |
| Data source | Notion API (notion-client 2.2.1) |
| Frontend | Bootstrap 5 (local), vanilla JS |
| Markdown | python-markdown |

---

## Setup

### Prerequisites
- Python 3.12+
- PostgreSQL running locally
- Notion integration with access to your project and task databases
- Anthropic API key

### Local installation

```bash
git clone https://github.com/your-username/planning-hub
cd planning_hub
python -m venv .venv
source .venv/bin/activate
pip install django anthropic notion-client markdown python-dotenv
```

Create a `.env` file:

```
NOTION_API_KEY=your_notion_key
ANTHROPIC_API_KEY=your_anthropic_key
```

Set up the database:

```bash
createdb planning_hub
python manage.py migrate
```

Run:

```bash
python manage.py runserver
```

Open http://127.0.0.1:8000

### Notion database IDs

In `projects/notion.py`, replace the two database IDs with your own:

```python
PROJECTS_DB = "your-projects-database-id"
TASKS_DB    = "your-tasks-database-id"
```

---

## DEMO_MODE

Runs the app on realistic fixture data — no Notion account or API keys required.

```bash
DEMO_MODE=true python manage.py runserver
```

All features work: dashboard, AI summary, event planner, task toggle, reschedule. Nothing is written to Notion in demo mode.
