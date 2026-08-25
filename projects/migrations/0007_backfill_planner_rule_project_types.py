# Generated for #105 review follow-up

from django.db import migrations

# A snapshot of INITIAL_RULES (projects/rules.py) as of #105, not an import
# of it — migrations must not depend on application code that can change
# after this migration has already run. Deployments that seeded PlannerRule
# before this PR added project_types have these five rows sitting at the new
# field's default ([], "applies to every type") instead of the scoping the
# PR intends; this backfills that gap by matching on the rule text.
KNOWN_PROJECT_TYPES = {
    "Bei Konzertveranstaltungen GEMA-Meldung einplanen — nicht bei Gottesdiensten": [
        "konzert"
    ],
    "Bei Konzerten Plakat als Standard voraussetzen; bei Gottesdiensten genügt ein Hausausdruck": [
        "konzert"
    ],
    "Vorverkauf nur bei größeren Konzerten relevant": ["konzert"],
    "Bei Recruiting / Personalplanung: Stellenausschreibung, Bewerbungsschluss, "
    "Interview-Runden, Referenzcheck und Angebot einplanen": ["recruiting"],
}


def backfill_project_types(apps, schema_editor):
    PlannerRule = apps.get_model("projects", "PlannerRule")
    for text, project_types in KNOWN_PROJECT_TYPES.items():
        # Only touch rows still at the field default — a maintainer who
        # already hand-assigned project_types via the API in the meantime
        # keeps her own choice.
        PlannerRule.objects.filter(text=text, project_types=[]).update(
            project_types=project_types
        )


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("projects", "0006_plannerrule_project_types"),
    ]

    operations = [
        migrations.RunPython(backfill_project_types, noop),
    ]
