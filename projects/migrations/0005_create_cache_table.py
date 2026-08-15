from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    # createcachetable is idempotent — safe to run on every migrate, in
    # production, in demo, and in the test database the runner builds from
    # migrations alone (see #52's implementation plan).
    call_command("createcachetable")


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0004_remove_task_project_remove_tasktemplate_project_type_and_more"),
    ]

    operations = [
        migrations.RunPython(create_cache_table, migrations.RunPython.noop),
    ]
