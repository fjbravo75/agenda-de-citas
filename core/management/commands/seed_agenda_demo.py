from django.core.management.base import BaseCommand

from core.demo_reset import AgendaDemoResetService


class Command(BaseCommand):
    help = "Seed the shared demo dataset for Agenda de Citas."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Accepted for backwards compatibility. The command already recreates the demo dataset from scratch.",
        )

    def handle(self, *args, **options):
        summary = AgendaDemoResetService().run()
        self.stdout.write(self.style.SUCCESS(summary.as_message(prefix="Agenda demo loaded")))
