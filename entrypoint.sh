#!/bin/sh
set -e

mkdir -p /app/data

echo "Running migrations..."
python manage.py migrate --noinput

# Idempotent (bails out once already seeded, tracked separately from
# PlannerRule's row count so deleting every rule via the UI doesn't
# resurrect the defaults on the next deploy), so it's safe on every start,
# including the demo container — where it runs but has no effect, since
# demo mode reads the session backend, not this table.
echo "Seeding planner rules..."
python manage.py seed_rules

echo "Collecting static files..."
python manage.py collectstatic --noinput

echo "Starting gunicorn..."
# Threads, not just processes: nearly every slow request here is *waiting* on
# Claude or Notion rather than computing, and a sync worker is blocked whole
# for that wait. A thread holds a waiting request for a few hundred KB; a
# worker costs a full Python process (~140 MB measured on the demo host), so
# concurrency is far cheaper bought in threads. Safe to do: notion._client()
# builds a fresh client per call and none of notion.py, ai.py or planner.py
# keeps mutable module-level state.
#
# Overridable per host because both stacks share this file while their
# machines do not: the demo VPS has 2 cores, the production Mac Mini 8. The
# defaults are the old sizing plus threads, so an unset environment behaves
# like before except for the added concurrency.
exec gunicorn planning_hub.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-2}" \
    --worker-class gthread \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout 120
