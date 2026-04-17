from django.core.management.base import BaseCommand

from core.demo_reset import AgendaDemoResetService


class Command(BaseCommand):
    help = "Reset the public demo dataset to its stable base state."

    def handle(self, *args, **options):
        summary = AgendaDemoResetService().run()
        self.stdout.write(self.style.SUCCESS(summary.as_message(prefix="Agenda demo reset")))
