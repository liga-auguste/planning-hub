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
exec gunicorn planning_hub.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 2 \
    --timeout 120
