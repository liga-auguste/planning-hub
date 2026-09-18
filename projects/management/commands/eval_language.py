from django.core.management.base import BaseCommand

from projects.language_eval import CASE_RUNNERS, format_report, run_eval


class Command(BaseCommand):
    help = "Run the language-consistency eval against the six live Claude touchpoints"

    def add_arguments(self, parser):
        parser.add_argument(
            "--only",
            help="Comma-separated touchpoint keys to run (a,b,c,d,e,f). Runs all six if omitted.",
        )

    def handle(self, *args, **kwargs):
        only = None
        if kwargs.get("only"):
            only = [key.strip() for key in kwargs["only"].split(",") if key.strip()]
            unknown = [key for key in only if key not in CASE_RUNNERS]
            if unknown:
                self.stderr.write(f"Unknown touchpoint key(s): {', '.join(unknown)}")
                return
        results = run_eval(only=only)
        self.stdout.write(format_report(results))
