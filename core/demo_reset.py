from dataclasses import dataclass
from datetime import datetime, time, timedelta
from itertools import cycle
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from core.day_availability import DayAvailabilityResolver
from core.models import (
    AGENDA_SLOT_TIMES,
    AgendaSettings,
    Appointment,
    AvailabilityBlock,
    BusinessSettings,
    Client,
    ManualClosure,
    Service,
    agenda_end_at_for_slot,
)


DEMO_TIMEZONE = ZoneInfo("Europe/Madrid")
DEMO_FULL_NAME = "Demo Estudio Norte"

DEMO_BUSINESS_SETTINGS = {
    "business_name": "Estudio Norte Peluquería",
    "phone": "952 48 36 20",
    "email": "hola@estudionorte.demo",
    "address": "Calle Primavera 18",
    "city": "Málaga",
    "tax_id": "B29765431",
}

DEMO_SERVICE_DEFINITIONS = (
    ("Corte mujer", 45, "#9C6644"),
    ("Lavar y peinar", 45, "#C97B63"),
    ("Tinte raíz", 90, "#8C5E58"),
    ("Color completo", 120, "#B56576"),
    ("Mechas", 150, "#E09F3E"),
    ("Corte y peinado", 60, "#6C8EAD"),
    ("Corte hombre", 30, "#355070"),
    ("Corte + barba", 45, "#4D908E"),
    ("Arreglo de barba", 30, "#577590"),
    ("Corte infantil", 30, "#90BE6D"),
    ("Tratamiento capilar", 60, "#7B6D8D"),
    ("Recogido / peinado especial", 75, "#D17A22"),
)

ACTIVE_REGULAR_CLIENTS = (
    "Lucia Martin",
    "Carmen Ruiz",
    "Paula Navarro",
    "Elena Torres",
    "Sofia Jimenez",
    "Marta Leon",
    "Alba Castro",
    "Nuria Molina",
    "Aitana Rojas",
    "Claudia Vega",
    "Marina Serrano",
    "Sara Campos",
    "Laura Prieto",
    "Andrea Vidal",
    "Noelia Cano",
    "Beatriz Lozano",
    "Javier Romero",
    "Daniel Ortega",
    "Alejandro Gil",
    "Carlos Fuentes",
    "Miguel Arias",
    "Antonio Vera",
    "Raul Mendez",
    "Pablo Nuñez",
)

ACTIVE_OCCASIONAL_CLIENTS = (
    "Irene Soler",
    "Patricia Esteban",
    "Veronica Rey",
    "Cristina Cabrera",
    "Monica Benitez",
    "Hugo Pastor",
    "Ivan Salas",
    "Adrian Ponce",
)

ARCHIVED_CLIENTS = (
    "Silvia Dominguez",
    "Rocio Herrera",
    "Tomas Gallego",
    "Joaquin Roldan",
)

DEMO_APPOINTMENT_PLAN = (
    (-24, "CCX", ("09:00", "12:00", "17:00")),
    (-20, "CCC", ("10:00", "13:00", "18:00")),
    (-16, "CCP", ("09:00", "11:00", "16:00")),
    (-12, "CCC", ("10:00", "12:00", "17:00")),
    (-8, "CCP", ("09:00", "13:00", "18:00")),
    (-6, "CCP", ("10:00", "12:00", "16:00")),
    (-4, "CCC", ("09:00", "11:00", "17:00")),
    (-2, "CCX", ("10:00", "13:00", "18:00")),
    (0, "CCP", ("09:00", "12:00", "17:00")),
    (1, "CCP", ("10:00", "13:00", "18:00")),
    (3, "CCC", ("09:00", "11:00", "16:00")),
    (5, "CPX", ("10:00", "12:00", "17:00")),
    (7, "CCP", ("09:00", "13:00", "18:00")),
    (10, "PPX", ("10:00", "12:00", "16:00")),
    (14, "CCC", ("09:00", "11:00", "17:00")),
    (20, "CCP", ("10:00", "13:00", "18:00")),
)


@dataclass(frozen=True)
class AgendaDemoResetSummary:
    service_count: int
    client_count: int
    manual_closure_count: int
    block_count: int
    appointment_count: int

    def as_message(self, *, prefix):
        return (
            f"{prefix}: "
            f"business=1, "
            f"user=1, "
            f"services={self.service_count}, "
            f"clients={self.client_count}, "
            f"manual_closures={self.manual_closure_count}, "
            f"blocks={self.block_count}, "
            f"appointments={self.appointment_count}."
        )


class AgendaDemoResetService:
    def run(self):
        reference_day = self._reference_day()

        with transaction.atomic():
            self._reset_demo_scope()
            self._configure_agenda_settings()
            self._configure_business_settings()
            self._configure_demo_user()
            services = self._create_services()
            clients = self._create_clients()
            manual_closures = self._create_manual_closures(reference_day)
            block_count = self._create_availability_blocks(reference_day)
            appointment_count = self._create_appointments(reference_day, clients, services)

        return AgendaDemoResetSummary(
            service_count=len(services),
            client_count=len(clients),
            manual_closure_count=len(manual_closures),
            block_count=block_count,
            appointment_count=appointment_count,
        )

    def _reference_day(self):
        return timezone.now().astimezone(DEMO_TIMEZONE).date()

    def _reset_demo_scope(self):
        AvailabilityBlock.objects.all().delete()
        Appointment.objects.all().delete()
        ManualClosure.objects.all().delete()
        Service.objects.all().delete()
        Client.objects.all().delete()

    def _configure_agenda_settings(self):
        AgendaSettings.objects.update_or_create(
            pk=1,
            defaults={
                "saturdays_non_working": True,
                "sundays_non_working": True,
            },
        )

    def _configure_business_settings(self):
        BusinessSettings.objects.update_or_create(
            pk=1,
            defaults=DEMO_BUSINESS_SETTINGS,
        )

    def _configure_demo_user(self):
        user_model = get_user_model()
        user, _created = user_model.objects.update_or_create(
            username=settings.DEMO_ACCESS_USERNAME,
            defaults={
                "email": settings.DEMO_ACCESS_USERNAME,
                "first_name": "Demo",
                "last_name": "Estudio Norte",
                "is_active": True,
                "is_staff": False,
                "is_superuser": False,
            },
        )
        user.set_password(settings.DEMO_ACCESS_PASSWORD)
        if hasattr(user, "full_name"):
            user.full_name = DEMO_FULL_NAME
        user.save()
        return user

    def _create_services(self):
        services = {}
        for name, duration_minutes, color in DEMO_SERVICE_DEFINITIONS:
            service = Service.objects.create(
                name=name,
                description=f"Duracion orientativa demo: {duration_minutes} min.",
                color=color,
                is_active=True,
            )
            services[name] = service
        return services

    def _create_clients(self):
        clients = {}
        for name in ACTIVE_REGULAR_CLIENTS:
            clients[name] = Client.objects.create(
                name=name,
                phone=self._demo_phone(name),
                email=self._demo_email(name),
                notes="Cliente habitual demo.",
                is_archived=False,
            )

        for name in ACTIVE_OCCASIONAL_CLIENTS:
            clients[name] = Client.objects.create(
                name=name,
                phone=self._demo_phone(name),
                email=self._demo_email(name),
                notes="Cliente ocasional demo.",
                is_archived=False,
            )

        for name in ARCHIVED_CLIENTS:
            clients[name] = Client.objects.create(
                name=name,
                phone=self._demo_phone(name),
                email=self._demo_email(name),
                notes="Cliente archivado demo.",
                is_archived=True,
            )

        return clients

    def _create_manual_closures(self, reference_day):
        one_day_closure = ManualClosure.objects.create(
            start_date=self._seedable_day_for_offset(reference_day, -18),
            end_date=self._seedable_day_for_offset(reference_day, -18),
            reason_type=ManualClosure.ReasonType.BUSINESS_CLOSURE,
            label="Inventario y mantenimiento",
            notes="Cierre demo para mantenimiento interno del local.",
        )

        range_start = self._seedable_day_for_offset(reference_day, 15)
        range_end = self._seedable_day_for_offset(reference_day, 16)
        if range_end < range_start:
            range_start, range_end = range_end, range_start

        training_closure = ManualClosure.objects.create(
            start_date=range_start,
            end_date=range_end,
            reason_type=ManualClosure.ReasonType.BUSINESS_CLOSURE,
            label="Formacion avanzada de color",
            notes="Cierre demo por formacion interna del equipo.",
        )

        return [one_day_closure, training_closure]

    def _create_availability_blocks(self, reference_day):
        occupied_slots_by_day = self._planned_appointment_slots(reference_day)
        block_definitions = (
            (self._seedable_day_for_offset(reference_day, -11), "16:00", "Bloqueo puntual"),
            (self._seedable_day_for_offset(reference_day, 2), "13:00", "Bloqueo puntual"),
            (self._seedable_day_for_offset(reference_day, 17), "11:00", "Bloqueo puntual"),
        )

        created = 0
        for day, preferred_slot_time, label in block_definitions:
            slot_time = self._pick_available_block_slot(
                day,
                preferred_slot_time,
                occupied_slots_by_day,
            )
            AvailabilityBlock.objects.create(day=day, slot_time=slot_time, label=label)
            created += 1
        return created

    def _create_appointments(self, reference_day, clients, services):
        client_order = self._appointment_client_order()
        archived_client_order = list(ARCHIVED_CLIENTS)
        client_cycle = cycle(client_order)
        service_cycle = cycle(name for name, _duration, _color in DEMO_SERVICE_DEFINITIONS)

        created = 0
        archived_remaining = archived_client_order[:]

        for index, (offset, status_pattern, slot_times) in enumerate(DEMO_APPOINTMENT_PLAN):
            target_day = self._seedable_day_for_offset(reference_day, offset)
            for slot_index, (status_key, slot_time) in enumerate(zip(status_pattern, slot_times, strict=True)):
                if archived_remaining and index < 2:
                    client_name = archived_remaining.pop(0)
                else:
                    client_name = next(client_cycle)
                service_name = next(service_cycle)
                self._create_single_appointment(
                    day=target_day,
                    slot_time=slot_time,
                    client=clients[client_name],
                    service=services[service_name],
                    status_key=status_key,
                    internal_notes=self._build_internal_notes(status_key, service_name, slot_index),
                )
                created += 1
        return created

    def _create_single_appointment(self, *, day, slot_time, client, service, status_key, internal_notes):
        status = {
            "C": Appointment.Status.CONFIRMED,
            "P": Appointment.Status.PENDING,
            "X": Appointment.Status.CANCELLED,
        }[status_key]

        start_at = timezone.make_aware(
            datetime.combine(day, time.fromisoformat(slot_time)),
            timezone.get_current_timezone(),
        )
        persisted_status = Appointment.Status.CONFIRMED if status == Appointment.Status.CANCELLED else status

        appointment = Appointment.objects.create(
            client=client,
            start_at=start_at,
            end_at=agenda_end_at_for_slot(start_at),
            status=persisted_status,
            internal_notes=internal_notes,
        )
        appointment.services.set([service])

        if status == Appointment.Status.CANCELLED:
            appointment.status = Appointment.Status.CANCELLED
            appointment.save(update_fields=["status"])

        return appointment

    def _build_internal_notes(self, status_key, service_name, slot_index):
        if status_key == "P":
            return "Pendiente de confirmacion telefonica."
        if status_key == "X":
            return "Cancelada por reajuste de agenda demo."
        if slot_index == 2 and service_name in {"Color completo", "Mechas", "Recogido / peinado especial"}:
            return "Reserva demo de servicio de mayor dedicacion."
        return ""

    def _appointment_client_order(self):
        return list(ACTIVE_REGULAR_CLIENTS) + list(ACTIVE_OCCASIONAL_CLIENTS)

    def _planned_appointment_slots(self, reference_day):
        occupied_slots_by_day = {}
        for offset, _status_pattern, slot_times in DEMO_APPOINTMENT_PLAN:
            day = self._seedable_day_for_offset(reference_day, offset)
            occupied_slots_by_day.setdefault(day, set()).update(slot_times)
        return occupied_slots_by_day

    def _pick_available_block_slot(self, day, preferred_slot_time, occupied_slots_by_day):
        occupied_slots = occupied_slots_by_day.setdefault(day, set())
        if preferred_slot_time not in occupied_slots:
            occupied_slots.add(preferred_slot_time)
            return preferred_slot_time

        for slot_time in AGENDA_SLOT_TIMES:
            if slot_time not in occupied_slots:
                occupied_slots.add(slot_time)
                return slot_time

        raise CommandError(
            f"Unable to place demo availability block on {day.isoformat()} without colliding with appointments."
        )

    def _seedable_day_for_offset(self, reference_day, offset):
        target_day = reference_day + timedelta(days=offset)
        lower_bound = reference_day - timedelta(days=28)
        upper_bound = reference_day + timedelta(days=28)
        step = 1 if offset >= 0 else -1

        while lower_bound <= target_day <= upper_bound:
            if DayAvailabilityResolver.resolve_for_global_agenda(target_day).is_working_day:
                return target_day
            target_day += timedelta(days=step or 1)

        fallback_day = reference_day
        while True:
            if DayAvailabilityResolver.resolve_for_global_agenda(fallback_day).is_working_day:
                return fallback_day
            fallback_day += timedelta(days=1)

    def _demo_phone(self, name):
        normalized = sum(ord(character) for character in name if character.isalpha())
        suffix = f"{normalized % 10000:04d}"
        return f"+34 6{suffix[:3]} {suffix[1:]}"

    def _demo_email(self, name):
        slug = (
            name.lower()
            .replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
            .replace("ñ", "n")
            .replace(" ", ".")
        )
        return f"{slug}@estudionorte.demo"
