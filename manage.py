#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys

from dotenv import dotenv_values, load_dotenv

from planning_hub.env import apply_credentials

# See planning_hub/env.py: .env fills in what the environment is missing, and
# its credentials additionally beat what is already there. Switches like
# DEMO_MODE stay overridable per invocation.
#
# This file DOES run in both containers — entrypoint.sh calls migrate,
# seed_rules and collectstatic before gunicorn starts. What makes the rule
# harmless there is one line: `.env` is in .dockerignore, and neither compose
# file bind-mounts the project directory (both mount named volumes only), so
# there is no file for load_dotenv to read and the whole block is a no-op.
# Compose supplies the variables as container environment via
# env_file/environment — the layer this rule demotes for credentials. If a
# `.env` ever reached /app, a developer's key would outrank the
# deployment's; DockerignoreProtectsTheDeploymentsTest pins that line.
load_dotenv()
apply_credentials(os.environ, dotenv_values())


def main():
    """Run administrative tasks."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "planning_hub.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
