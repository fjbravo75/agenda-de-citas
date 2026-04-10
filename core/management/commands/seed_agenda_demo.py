from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.day_availability import DayAvailabilityResolver
from core.models import Appointment, AvailabilityBlock, Client, Service


class Command(BaseCommand):
    help = "Create a reproducible demo dataset for the agenda."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Delete existing clients, services, appointments and blocks before recreating the demo dataset.",
        )

    def handle(self, *args, **options):
        reset = options["reset"]

        if not reset and self._has_existing_data():
            raise CommandError(
                "Existing agenda data found. Re-run with --reset if you want to replace it with the demo dataset."
            )

        with transaction.atomic():
            if reset:
                AvailabilityBlock.objects.all().delete()
                Appointment.objects.all().delete()
                Service.objects.all().delete()
                Client.objects.all().delete()

            dataset = self._build_dataset()
            clients = self._create_clients(dataset["clients"])
            services = self._create_services(dataset["services"])
            block_count = self._create_availability_blocks(dataset["availability_blocks"])
            created_appointments = self._create_appointments(dataset["appointments"], clients, services)

        self.stdout.write(
            self.style.SUCCESS(
                "Agenda demo loaded: "
                f"{len(clients)} clients, "
                f"{len(services)} services, "
                f"{block_count} blocks, "
                f"{created_appointments} appointments."
            )
        )

    def _has_existing_data(self):
        return (
            Client.objects.exists()
            or Service.objects.exists()
            or Appointment.objects.exists()
            or AvailabilityBlock.objects.exists()
        )

    def _next_working_days(self, start_day, *, count):
        working_days = []
        candidate_day = start_day

        while len(working_days) < count:
            if DayAvailabilityResolver.resolve_for_global_agenda(candidate_day).is_working_day:
                working_days.append(candidate_day)
            candidate_day += timedelta(days=1)

            if (candidate_day - start_day).days > 60:
                raise CommandError("Unable to find enough working days to build the demo agenda dataset.")

        return working_days

    def _build_dataset(self):
        working_days = self._next_working_days(timezone.localdate(), count=5)
        demo_days = {
            "today": working_days[0],
            "next_day": working_days[1],
            "two_days": working_days[2],
            "four_days": working_days[3],
            "one_week": working_days[4],
        }

        return {
            "clients": [
                {"name": "Paula Martin", "phone": "+34 600 111 111", "email": "paula@example.com"},
                {"name": "Diego Lara", "phone": "+34 600 222 222", "email": "diego@example.com"},
                {"name": "Marta Leon", "phone": "+34 600 333 333", "email": "marta@example.com"},
                {"name": "Carlos Ruiz", "phone": "+34 600 444 444", "email": "carlos@example.com"},
                {"name": "Sofia Marquez", "phone": "+34 600 555 555", "email": "sofia@example.com"},
                {"name": "Lucia Gomez", "phone": "+34 600 666 666", "email": "lucia@example.com"},
                {"name": "Raul Soto", "phone": "+34 600 777 777", "email": "raul@example.com"},
            ],
            "services": [
                {"name": "Fisio inicial", "duration_minutes": 60, "color": "#3158D7"},
                {"name": "Revision", "duration_minutes": 45, "color": "#2E7A58"},
                {"name": "Seguimiento", "duration_minutes": 30, "color": "#A06A11"},
                {"name": "Evaluacion", "duration_minutes": 60, "color": "#AE4C42"},
                {"name": "Control", "duration_minutes": 30, "color": "#6D7A8C"},
            ],
            "availability_blocks": [
                {"day": demo_days["today"], "slot_time": "16:00", "label": "Bloqueo interno"},
                {"day": demo_days["next_day"], "slot_time": "11:00", "label": "Bloqueo puntual"},
                {"day": demo_days["one_week"], "slot_time": "09:00", "label": "Bloqueo puntual"},
            ],
            "appointments": [
                {
                    "client": "Paula Martin",
                    "service": "Revision",
                    "day": demo_days["today"],
                    "start_time": time(9, 0),
                    "status": Appointment.Status.CONFIRMED,
                },
                {
                    "client": "Diego Lara",
                    "service": "Control",
                    "day": demo_days["today"],
                    "start_time": time(10, 0),
                    "status": Appointment.Status.PENDING,
                },
                {
                    "client": "Marta Leon",
                    "service": "Seguimiento",
                    "day": demo_days["today"],
                    "start_time": time(12, 0),
                    "status": Appointment.Status.CONFIRMED,
                },
                {
                    "client": "Raul Soto",
                    "service": "Revision",
                    "day": demo_days["today"],
                    "start_time": time(17, 0),
                    "status": Appointment.Status.CANCELLED,
                    "internal_notes": "Cancelada por el cliente",
                },
                {
                    "client": "Carlos Ruiz",
                    "service": "Revision",
                    "day": demo_days["next_day"],
                    "start_time": time(9, 0),
                    "status": Appointment.Status.CONFIRMED,
                },
                {
                    "client": "Sofia Marquez",
                    "service": "Control",
                    "day": demo_days["next_day"],
                    "start_time": time(10, 0),
                    "status": Appointment.Status.PENDING,
                },
                {
                    "client": "Lucia Gomez",
                    "service": "Evaluacion",
                    "day": demo_days["two_days"],
                    "start_time": time(17, 0),
                    "status": Appointment.Status.CONFIRMED,
                },
                {
                    "client": "Marta Leon",
                    "service": "Fisio inicial",
                    "day": demo_days["four_days"],
                    "start_time": time(9, 0),
                    "status": Appointment.Status.CONFIRMED,
                },
                {
                    "client": "Paula Martin",
                    "service": "Seguimiento",
                    "day": demo_days["one_week"],
                    "start_time": time(12, 0),
                    "status": Appointment.Status.PENDING,
                },
            ],
        }

    def _create_clients(self, client_definitions):
        clients = {}
        for definition in client_definitions:
            client = Client.objects.create(**definition)
            clients[client.name] = client
        return clients

    def _create_services(self, service_definitions):
        services = {}
        for definition in service_definitions:
            service = Service.objects.create(**definition)
            services[service.name] = service
        return services

    def _create_availability_blocks(self, block_definitions):
        created = 0
        for definition in block_definitions:
            AvailabilityBlock.objects.create(**definition)
            created += 1
        return created

    def _create_appointments(self, appointment_definitions, clients, services):
        current_tz = timezone.get_current_timezone()
        created = 0
        for definition in appointment_definitions:
            service = services[definition["service"]]
            start_at = timezone.make_aware(
                datetime.combine(definition["day"], definition["start_time"]),
                current_tz,
            )
            target_status = definition["status"]
            appointment = Appointment.objects.create(
                client=clients[definition["client"]],
                start_at=start_at,
                end_at=start_at + timedelta(minutes=service.duration_minutes),
                status=(
                    Appointment.Status.CONFIRMED
                    if target_status == Appointment.Status.CANCELLED
                    else target_status
                ),
                internal_notes=definition.get("internal_notes", ""),
            )
            appointment.services.set([service])
            if target_status == Appointment.Status.CANCELLED:
                appointment.status = Appointment.Status.CANCELLED
                appointment.save()
            created += 1
        return created
