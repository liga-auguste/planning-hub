# Changelog

## Session 3 — 04.08.2026

### Features

**Task-Toggle (tasks abhaken)**
- `toggle_task_view` in `views.py`: POST-Endpunkt der Notion API aufruft
- URL `task/<id>/toggle/` in `urls.py`
- Checkbox-Button im Dashboard per JavaScript fetch — kein Seitenreload, DOM-Update sofort
- Im DEMO_MODE: Button funktioniert visuell ohne Notion-Schreibzugriff

**Task-Datum bearbeiten**
- `update_task_date()` in `notion.py`
- `reschedule_task_view` in `views.py`: POST mit `{"date": "YYYY-MM-DD"}`
- Klick auf Datum öffnet nativen Datepicker (`showPicker()`)
- "→ heute"-Button bei überfälligen Tasks

**DEMO_MODE**
- Flag in `settings.py` via Umgebungsvariable `DEMO_MODE=true`
- `demo_data.py`: 5 realistische Kirchenmusik-Projekte mit vollständigem Aufgabenzyklus, 3 historische Projekte für KI-Kontext
- Dashboard, Toggle und Reschedule respektieren den Flag
- Planner (`planner_views.py`) nutzt Demo-History statt Notion, schreibt nicht zurück

**KI-Wochenübersicht — verbesserter Output**
- Prompt umgeschrieben: Assistenz-Stimme statt Auflistungsmaschine, ein Einleitungssatz pro Projekt
- `_fix_ai_markdown()` in `views.py`: konvertiert Plaintext-Zeilen nach verschachteltem Markdown (4-Leerzeichen-Einrückung) damit Python-Markdown korrekt nested lists rendert
- Projektnamen im KI-Output als Links zur jeweiligen Projektansicht (via `project_map` JSON + JavaScript)
- Task-Items eingerückt und kleiner dargestellt (`.ai-task-item` CSS-Klasse via JavaScript gesetzt)

**Planner im Dashboard zugänglich**
- "Neue Veranstaltung"-Link in der Sidebar

### UI / Design

**Layout**
- Resizable Sidebar: Drag-Handle am rechten Rand, Breite in `localStorage` gespeichert
- Sidebar active state (`.sidebar-item.active`) nun sichtbar hervorgehoben
- Sidebar-Projektfarben: monochrom grau, nur überfällige Projekte in Rot — kein generisches Blau/Orange mehr
- Kontext-Badge (Orga/Musik/Kommunikation) als Tag hinter dem Tasknamen
- Hover-Outline auf Checkbox-Buttons
- KI-Card borderless: kein weißer Rahmen, nur Whitespace-Trennung
- Projekttitel-Heading "Übersicht" mit Datum zurück im Content-Bereich
- Saubere Typografie-Hierarchie: Projektname fett, Tasks kleiner/grau/eingerückt
- Projektansicht: `event_date_display` im Projekt-Header sichtbar

**Entfernt**
- Sticky Topbar mit Breadcrumb und Navigations-Pfeilen (redundant zur Sidebar)
- Überladene Statusfarben aus Notion (blau, orange, grün) — einheitlich grau
