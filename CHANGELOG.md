# Changelog

## Session 6 — 05.08.2026

### Demo-Modus: Generalisierung & Onboarding

**Landing Page (`landing.html`)**
- 4 Kacheln mit Vorausfüllung und Typangabe: Konzert / Event, Hochzeit / Feier, Recruiting, Eigenes Projekt
- `?prefill=...&type=...`-Parameter: Kachel füllt Planner-Eingabefeld vor, speichert Projekttyp in Session
- Link „Beispielprojekte ansehen →" führt zum Demo-Dashboard
- `views.index`: in DEMO_MODE → landing.html, sonst → redirect dashboard

**KI-Prompts generalisiert**
- Kirchenmusik-spezifische Rolle aus Prompt entfernt; Claude erkennt Projekttyp selbst
- Domänen-Regeln (GEMA, Musikerverträge, Gottesdienst) bleiben erhalten, aber konditionell: „Wende Regeln an, sofern sie zum Projekttyp passen"
- Planner-Questions: Freitext-Antwortfeld statt vorgefertigter Kirchenmusik-Felder
- Event-Datum-Parser: erkennt Datum ohne Jahr, berechnet nächstes Vorkommen
- KI-Referenz auf historische Kirchenmusik-Daten wird intern genutzt, nicht im Output erwähnt

**Editierbare Planungsregeln (`PlannerRule`-Modell)**
- Neues Django-Modell: `PlannerRule` (text, active, order)
- Admin-UI `planner_rules.html`: Drag-and-Drop Reihenfolge (Sortable.js), Toggle-Switch, Inline-Edit (contenteditable), Löschen
- Alle Operationen per AJAX (toggle, update, delete, reorder) — kein Seiten-Reload
- CRUD-Endpunkte in `planner_urls.py` (`/planner/regeln/`)
- `seed_rules` Management-Command: legt 5 initiale Regeln an
- Regeln werden bei Plan-Generierung konditionell injiziert (`_get_active_rules()`)

**Session-basierte Demo-Pläne**
- `planner_create` in DEMO_MODE: Plan wird in Django-Session gespeichert (kein geteilter DB-State)
- `_build_session_project()`: konvertiert Session-Dict in Projekt-Format kompatibel mit Dashboard
- Dashboard: wenn Session-Plan vorhanden → zeigt diesen Plan + KI-Zusammenfassung
- KI-Zusammenfassung wird in Session gecacht, bei neuem Plan invalidiert
- Task-Toggle (`toggle_session_task`) aktualisiert Session-Plan direkt

**Plan-Download als Markdown**
- `download_plan`-View: generiert `.md`-Datei mit Checkboxen, Kontexten, Datum, Zieldatum
- Datei enthält „Tipp für KI-Tools" (Claude, ChatGPT, etc.) am Ende
- `Content-Disposition: attachment` — Browser-Download, kein Seitenaufruf
- Loggt `plan_downloaded`-Event in DemoEvent

**Mein Plan-Seite (`my_plan.html`)**
- Eigene Route `/mein-plan/` für Session-Plan-Inhaber
- Zeigt Fortschrittsbalken, Task-Liste mit Toggle, KI-Zusammenfassung
- Download- und „Neu planen"-Button im Header
- Weiterleitung zum Dashboard-Flow (nicht mehr Endpunkt der Reise)

**Anonyme Nutzungsstatistiken (`DemoEvent`-Modell)**
- Neues Modell: `DemoEvent` (event_type, project_type, task_count, created_at)
- Kein Inhalt, keine personenbezogene Daten — DSGVO-konform
- Events: `plan_generated` (bei Planner-Abschluss), `plan_downloaded`
- Stats-Seite `/stats/`: 3 Karten (generiert / heruntergeladen / Download-Rate), Balkendiagramm nach Projekttyp, Tagesverlauf letzte 14 Tage

### Migrationen
- `0002_plannerrule.py`
- `0003_demoevent.py`

### Offen / Nächste Schritte

**Time-Lapse-Simulation (geplant)**
Idee: In DEMO_MODE simulierbare Zeitsprünge auf dem Dashboard — „Wie sieht der Plan aus, wenn heute 4 Wochen vor dem Termin wäre?"
- Preset-Buttons: Heute / 4 Wochen vorher / 1 Woche vorher / 2 Tage vorher
- Simuliertes Datum in Session gespeichert
- Dashboard berechnet Urgency, überfällige Tasks und KI-Zusammenfassung auf Basis dieses Datums neu
- KI simuliert realistischen Fortschritt (welche Tasks wären zu diesem Zeitpunkt erledigt?)
- Ziel: Recruiter oder Besucher sieht den Mehrwert des Tools in einem einzigen Blick

---

## Session 5 — 04.08.2026 (Abend)

### Demo-Stack auf Mac Mini

- Zweiter Docker-Stack geklont nach `~/Apps/planning-hub-demo/`
- Eigene `.env`: `DEMO_MODE=true`, `ANTHROPIC_API_KEY` gesetzt, keine DB-Felder, kein `NOTION_API_KEY`
- `docker-compose.yml` im Demo-Ordner: nginx auf Port 8081 statt 80
- `nginx-demo.conf`: kein Basic Auth, Rate-Limiting (10 Anfragen/Minute pro IP, Burst 5)

### Öffentliche URL via eigener Domain

- CNAME-Eintrag bei All-Inkl: `planninghub.ligaauguste.de` → `ckqqd45579i9t7z0.myfritz.net`
- Fritz!Box DNS-Rebind-Schutz: `planninghub.ligaauguste.de` als Ausnahme eingetragen
- Fritz!Box Portfreigabe geändert: extern 80 → intern 8081 (Mac Mini, Demo-Stack)
- `ALLOWED_HOSTS` in Demo-`.env`: `ckqqd45579i9t7z0.myfritz.net localhost planninghub.ligaauguste.de`
- DNS-Propagierung noch ausstehend — testen sobald fertig

### Aktueller Stand beider Stacks
- **Live:** `~/Apps/planning-hub/` — Port 80 intern, nur per VPN via `http://192.168.178.121`, passwortgeschützt
- **Demo:** `~/Apps/planning-hub-demo/` — Port 8081 intern / 80 extern, öffentlich via `http://planninghub.ligaauguste.de`

### Entwicklungs-Workflow
- Lokal entwickeln + testen (`python manage.py runserver`)
- Committen + pushen auf GitHub
- Auf Mac Mini: `git pull` + `docker compose up --build -d` in beiden Ordnern (`planning-hub` + `planning-hub-demo`)

### TODO
- DNS-Propagierung abwarten und `http://planninghub.ligaauguste.de` testen
- **GitHub Actions CD** einrichten: nach jedem Push automatisch per SSH auf Mac Mini → `git pull` + `docker compose up --build -d` in beiden Stacks

---

## Session 4 — 04.08.2026 (Abend)

### Deployment: Mac Mini (produktiv)

**Docker-Setup**
- `Dockerfile`: Python 3.12-slim, gunicorn als Produktions-Webserver
- `entrypoint.sh`: migrate → collectstatic → gunicorn beim Containerstart
- `docker-compose.yml`: drei Services — db (PostgreSQL 16), web (Django), nginx
- `nginx.conf`: Reverse Proxy, Static Files direkt aus Volume
- `requirements.txt`: pinnte Pakete inkl. gunicorn
- `settings.py`: SECRET_KEY, DEBUG, ALLOWED_HOSTS, DB-Verbindung aus Umgebungsvariablen
- `STATIC_ROOT` ergänzt für collectstatic
- `.env.example`: Vorlage für Produktions-Secrets

**Deployment auf Mac Mini (remote per SSH aus Lettland)**
- SSH-Key auf Mac Mini eingerichtet (ed25519, GitHub)
- Repo geklont nach `~/Apps/planning-hub`
- `.env` mit echten Keys befüllt (NOTION_API_KEY, ANTHROPIC_API_KEY, DB_PASSWORD, SECRET_KEY)
- Docker Compose gestartet: alle drei Container laufen
- App erreichbar unter `http://192.168.178.121` (MyFRITZ! ermöglicht Zugriff von außen)

**Passwortschutz**
- HTTP Basic Auth in nginx (`auth_basic`, `auth_basic_user_file`)
- `htpasswd`-Datei im nginx-Container angelegt
- Benutzername: `liga`

---

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

---

## Session 2 — 03.08.2026 (Abend)

### Features

**KI-Planungsassistent (`/planner/`)**
- `planner.py`: RAG-basierter 2-Schritt-KI-Flow — Schritt 1 generiert Klärungsfragen aus Veranstaltungsbeschreibung, Schritt 2 erzeugt JSON-Aufgabenplan
- Historische Projekte als Kontext: `get_historical_projects()` lädt abgeschlossene Notion-Projekte; Claude leitet typische Zeitabstände pro Task-Typ ab; Musik-zur-Marktzeit herausgefiltert (zu repetitiv)
- Domänen-Regeln im Prompt: GEMA (Konzert ja / Gottesdienst nein), Musikerverträge, Honorare, Plakatvarianten, Vorverkauf
- `planner_views.py`: 3-Schritt-Flow (Beschreiben → Klärungsfragen + Vereinbarungsfelder → Review → Notion-Schreiben)
- 24h-Caching für historische Daten: Ladetime 37 s → 0,01 s
- Review-Screen: editierbare Aufgabentabelle mit Datums-Picker, Sofort-Markierung für bereits vergangene Tasks
- `planner_urls.py`: eigene URL-Gruppe unter `/planner/`
- Templates: `planner_start.html`, `planner_questions.html`, `planner_review.html`

**Notion Write**
- `create_project(name, event_date)` → legt Projekt in Notion an, Status "geplant / mit Zeitplan"
- `create_tasks(project_id, tasks)` → legt Tasks an, verknüpft über Relation mit Projekt
- Nach Notion-Schreiben: Cache geleert, Redirect zum Dashboard

**Kontext-System (`ai.py`)**
- `TASK_KONTEXT`-Dict: Mapping Task-Name → Kontext (Planung / Büro / Graphiker / Kommunikation / Unterwegs / Vor Ort)
- `derive_kontext()`: leitet Kontext automatisch aus Task-Namen ab — kein manuelles Tagging in Notion nötig
- Workflow-Blöcke im KI-Prompt nach Kontext gruppiert (projektübergreifend)

---

## Session 1 — 03.08.2026 (Nachmittag)

### Initiales Setup

- Django 6.0 Projektgerüst (`planning_hub/`), PostgreSQL-Datenbankanbindung
- Bootstrap lokal eingebunden (kein CDN)
- `prototype.py`: erster Proof-of-Concept — Claude API mit hartcodierten Beispielprojekten
- Seed-Commands: `seed_data.py`, `seed_projects.py` (lokale Testdaten)

### Notion als Source of Truth

- `notion.py`: komplette Notion-API-Anbindung — liest Projekte und Tasks live
- Notion-Status-Mapping (To-Do / In Bearbeitung / Abgeschlossen) aus Originalwerten der Datenbank
- Lokale PostgreSQL-DB-Abfragen vollständig durch Notion-Abfragen ersetzt

### Dashboard (`dashboard.html` + `views.py`)

- Linear-inspiriertes Layout: Sidebar (Projekte nach Monat), Content-Bereich (Übersicht-Tab + Projekt-Detail-Tab)
- Filter: nur aktive Projekte (nicht abgeschlossen, nicht "kein Status erforderlich")
- Urgency-System: overdue / urgent / ok je Task und Projekt — roter/oranger Strich in der Sidebar
- Dots nach Notion-Status gefärbt, deutsches Datumsformat, Projektnamen ohne Jahreszahl
- Manueller Sync-Button (Cache invalidieren + neu laden)
- Caching: 8h LocMemCache für Notion-Abfragen

### KI-Wochenübersicht

- Streaming-Aufruf an Claude API (`claude-sonnet-4-6`)
- Kontext-bewusster Prompt: Workflow-Blöcke nach Kontext gruppiert
- Markdown-Rendering mit Python-`markdown`-Library
- `generate_weekly_summary()` in `ai.py`
