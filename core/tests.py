from datetime import date, datetime, time, timedelta
from io import StringIO
from unittest.mock import patch
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from .management.commands.sync_official_holidays import (
    BoeHolidayResolution,
    BoeNationalHolidaySyncService,
    BoeSyncError,
    OfficialHolidayImport,
    OfficialHolidaySyncResult,
)
from .day_availability import DayAvailabilityResolver
from .forms import (
    AgendaSettingsForm,
    AppointmentForm,
    BusinessSettingsForm,
    ManualClosureForm,
    OfficialHolidaySyncForm,
    ServiceForm,
)
from .models import (
    AgendaSettings,
    Appointment,
    AvailabilityBlock,
    BusinessSettings,
    Client,
    ManualClosure,
    OfficialHoliday,
    Service,
    agenda_end_at_for_slot,
    agenda_slot_operational_state_map,
)
from .views import _format_compact_day


class AgendaBaseTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.real_today = timezone.localdate()
        self.operational_day = self._next_working_day(include_today=True)
        self.review_service = Service.objects.create(name="Revision", color="#3158D7")
        self.control_service = Service.objects.create(name="Control", color="#2E7A58")
        self.primary_client = Client.objects.create(name="Claudia Real")
        self.secondary_client = Client.objects.create(name="Mario Real")
        self.tertiary_client = Client.objects.create(name="Nora Real")
        self.fourth_client = Client.objects.create(name="Lia Real")
        self.app_user = get_user_model().objects.create_user(
            username="agenda-operator",
            password="agenda-pass-123",
            is_staff=True,
            is_superuser=True,
        )

    def _build_appointment(self, client, service, target_day, start_time, status):
        start_at = timezone.make_aware(
            datetime.combine(target_day, start_time),
            timezone.get_current_timezone(),
        )
        return Appointment(
            client=client,
            start_at=start_at,
            end_at=agenda_end_at_for_slot(start_at),
            status=status,
        )

    def _create_appointment(self, client, service, target_day, start_time, status):
        appointment = self._build_appointment(client, service, target_day, start_time, status)
        appointment.save()
        appointment.services.set([service])
        return appointment

    def _appointment_service_ids(self, appointment):
        return list(appointment.services.order_by("pk").values_list("pk", flat=True))

    def _create_existing_cancelled_appointment(
        self,
        client,
        service,
        target_day,
        start_time,
        *,
        initial_status=Appointment.Status.CONFIRMED,
        internal_notes="",
    ):
        appointment = self._create_appointment(client, service, target_day, start_time, initial_status)
        appointment.status = Appointment.Status.CANCELLED
        appointment.internal_notes = internal_notes
        appointment.save()
        return appointment

    def _create_block(self, target_day, slot_time, label="Bloqueo puntual"):
        return AvailabilityBlock.objects.create(day=target_day, slot_time=slot_time, label=label)

    def login_app_user(self):
        self.client.force_login(self.app_user)

    def _next_weekday(self, weekday, *, start_day=None, include_today=False):
        base_day = start_day or getattr(self, "real_today", timezone.localdate())
        delta = (weekday - base_day.weekday()) % 7
        if delta == 0 and not include_today:
            delta = 7
        return base_day + timedelta(days=delta)

    def _next_working_day(self, *, start_day=None, include_today=False):
        return self._working_day_from(start_day=start_day, include_today=include_today, step=1)

    def _previous_working_day(self, *, start_day=None, include_today=False):
        return self._working_day_from(start_day=start_day, include_today=include_today, step=-1)

    def _working_day_from(self, *, start_day=None, include_today=False, step=1):
        base_day = start_day or getattr(self, "real_today", timezone.localdate())
        candidate_day = base_day if include_today else base_day + timedelta(days=step)

        while not DayAvailabilityResolver.resolve_for_global_agenda(candidate_day).is_working_day:
            candidate_day += timedelta(days=step)

        return candidate_day


class AuthenticatedAgendaBaseTestCase(AgendaBaseTestCase):
    def setUp(self):
        super().setUp()
        self.login_app_user()


class AppointmentSlotValidationTests(AgendaBaseTestCase):
    def test_operational_day_without_weekly_availability_uses_base_capacity_for_all_slots(self):
        today = self.operational_day

        slot_state_map = agenda_slot_operational_state_map(today)

        self.assertEqual(len(slot_state_map), 8)
        self.assertTrue(all(state["capacity"] == 3 for state in slot_state_map.values()))
        self.assertTrue(all(state["can_book"] for state in slot_state_map.values()))

    def test_valid_appointment_in_available_slot_can_be_created(self):
        today = self.operational_day

        appointment = self._build_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )
        appointment.save()

        self.assertEqual(Appointment.objects.count(), 1)

    def test_new_appointment_cannot_be_created_directly_as_cancelled(self):
        today = self.operational_day

        appointment = self._build_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CANCELLED,
        )

        with self.assertRaises(ValidationError) as raised:
            appointment.save()

        self.assertIn("no puede crearse ya cancelada", str(raised.exception))
        self.assertEqual(Appointment.objects.count(), 0)

    def test_appointments_keep_slot_based_validation_independent_from_service_catalog(self):
        today = self.operational_day

        first_appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        second_appointment = self._build_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(10, 0),
            Appointment.Status.PENDING,
        )

        second_appointment.save()
        first_appointment.refresh_from_db()

        self.assertEqual(first_appointment.end_at, agenda_end_at_for_slot(first_appointment.start_at))
        self.assertEqual(second_appointment.end_at, agenda_end_at_for_slot(second_appointment.start_at))
        self.assertEqual(Appointment.objects.count(), 2)
        self.assertEqual(Appointment.active_slot_appointments_count(today, "09:00"), 1)
        self.assertEqual(Appointment.active_slot_appointments_count(today, "10:00"), 1)

    def test_appointment_in_blocked_slot_is_rejected(self):
        today = self.operational_day
        self._create_block(today, "11:00", label="Bloqueo interno")

        appointment = self._build_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )

        with self.assertRaises(ValidationError) as raised:
            appointment.save()

        self.assertIn("bloqueado", str(raised.exception))

    def test_appointment_without_weekly_availability_can_be_created_on_operational_day(self):
        today = self.operational_day

        appointment = self._build_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )

        appointment.save()

        self.assertEqual(Appointment.objects.count(), 1)
        self.assertEqual(appointment.slot_time, "11:00")

    def test_multiple_appointments_are_allowed_until_slot_capacity(self):
        today = self.operational_day

        first = self._build_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )
        second = self._build_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )

        first.save()
        second.save()

        self.assertEqual(Appointment.objects.count(), 2)
        self.assertEqual(Appointment.active_slot_appointments_count(today, "11:00"), 2)

    def test_appointment_is_rejected_when_slot_capacity_is_exceeded(self):
        today = self.operational_day
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )
        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )

        fourth = self._build_appointment(
            self.fourth_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )

        with self.assertRaises(ValidationError) as raised:
            fourth.save()

        self.assertIn("capacidad maxima", str(raised.exception))

    def test_slot_with_capacity_three_accepts_three_active_appointments_and_rejects_a_fourth(self):
        today = self.operational_day

        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )
        third = self._build_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )
        fourth = self._build_appointment(
            self.fourth_client,
            self.control_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )

        third.save()
        self.assertEqual(Appointment.active_slot_appointments_count(today, "11:00"), 3)

        with self.assertRaises(ValidationError) as raised:
            fourth.save()

        self.assertIn("capacidad maxima", str(raised.exception))

    def test_appointment_on_non_working_saturday_is_rejected(self):
        selected_day = self._next_weekday(5)

        appointment = self._build_appointment(
            self.primary_client,
            self.review_service,
            selected_day,
            time(11, 0),
            Appointment.Status.PENDING,
        )

        with self.assertRaises(ValidationError) as raised:
            appointment.save()

        self.assertIn("La agenda no opera el dia seleccionado", str(raised.exception))
        self.assertIn("Sabado no laborable", str(raised.exception))

    def test_appointment_on_non_working_sunday_is_rejected(self):
        selected_day = self._next_weekday(6)

        appointment = self._build_appointment(
            self.primary_client,
            self.review_service,
            selected_day,
            time(11, 0),
            Appointment.Status.PENDING,
        )

        with self.assertRaises(ValidationError) as raised:
            appointment.save()

        self.assertIn("La agenda no opera el dia seleccionado", str(raised.exception))
        self.assertIn("Domingo no laborable", str(raised.exception))

    def test_appointment_on_manual_closure_is_rejected(self):
        selected_day = self._next_weekday(2)
        ManualClosure.objects.create(
            start_date=selected_day,
            end_date=selected_day,
            reason_type=ManualClosure.ReasonType.VACATION,
            label="Vacaciones de abril",
        )

        appointment = self._build_appointment(
            self.primary_client,
            self.review_service,
            selected_day,
            time(11, 0),
            Appointment.Status.PENDING,
        )

        with self.assertRaises(ValidationError) as raised:
            appointment.save()

        self.assertIn("La agenda no opera el dia seleccionado", str(raised.exception))
        self.assertIn("Vacaciones de abril", str(raised.exception))

    def test_existing_appointment_can_keep_same_closed_slot_for_follow_up_edits(self):
        selected_day = self._next_weekday(5)
        settings = AgendaSettings.get_solo()
        settings.saturdays_non_working = False
        settings.save()
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            selected_day,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )

        settings.saturdays_non_working = True
        settings.save()
        appointment.internal_notes = "Seguimiento permitido"
        appointment.save()

        appointment.refresh_from_db()
        self.assertEqual(appointment.internal_notes, "Seguimiento permitido")


class AgendaSettingsModelTests(TestCase):
    def test_get_solo_creates_and_reuses_the_global_singleton(self):
        first_settings = AgendaSettings.get_solo()
        second_settings = AgendaSettings.get_solo()

        self.assertEqual(first_settings.pk, 1)
        self.assertEqual(second_settings.pk, 1)
        self.assertEqual(AgendaSettings.objects.count(), 1)

    def test_second_singleton_record_is_rejected(self):
        AgendaSettings.get_solo()

        with self.assertRaises(ValidationError):
            AgendaSettings.objects.create(saturdays_non_working=False)

        self.assertEqual(AgendaSettings.objects.count(), 1)

    def test_official_holidays_are_non_working_by_default(self):
        settings = AgendaSettings.get_solo()

        self.assertTrue(settings.official_holidays_non_working)


class BusinessSettingsModelTests(TestCase):
    def test_get_solo_can_return_empty_without_creating_record(self):
        self.assertIsNone(BusinessSettings.get_solo())
        self.assertEqual(BusinessSettings.objects.count(), 0)

    def test_get_solo_can_create_and_reuse_fixed_singleton(self):
        first_settings = BusinessSettings.get_solo(create=True)
        second_settings = BusinessSettings.get_solo(create=True)

        self.assertEqual(first_settings.pk, 1)
        self.assertEqual(second_settings.pk, 1)
        self.assertEqual(BusinessSettings.objects.count(), 1)

    def test_second_singleton_record_is_rejected(self):
        BusinessSettings.get_solo(create=True)

        with self.assertRaises(ValidationError):
            BusinessSettings.objects.create(business_name="Otra empresa")

        self.assertEqual(BusinessSettings.objects.count(), 1)


class ManualClosureModelTests(TestCase):
    def test_manual_closure_rejects_an_inverted_date_range(self):
        with self.assertRaises(ValidationError) as raised:
            ManualClosure.objects.create(
                start_date=date(2026, 4, 12),
                end_date=date(2026, 4, 11),
                reason_type=ManualClosure.ReasonType.VACATION,
            )

        self.assertIn("fecha final", str(raised.exception))

    def test_manual_closure_rejects_overlapping_ranges(self):
        ManualClosure.objects.create(
            start_date=date(2026, 4, 14),
            end_date=date(2026, 4, 16),
            reason_type=ManualClosure.ReasonType.BUSINESS_CLOSURE,
            label="Cierre por reformas",
        )

        with self.assertRaises(ValidationError) as raised:
            ManualClosure.objects.create(
                start_date=date(2026, 4, 16),
                end_date=date(2026, 4, 18),
                reason_type=ManualClosure.ReasonType.OTHER,
                label="Otro cierre",
            )

        self.assertIn("solapa", str(raised.exception))


class OfficialHolidayModelTests(TestCase):
    def test_official_holiday_defaults_to_manual_source(self):
        holiday = OfficialHoliday.objects.create(day=date(2026, 4, 23), name="San Jorge")

        self.assertEqual(holiday.source, OfficialHoliday.Source.MANUAL)

    def test_official_holiday_rejects_duplicate_day(self):
        OfficialHoliday.objects.create(day=date(2026, 4, 23), name="San Jorge")

        with self.assertRaises(ValidationError) as raised:
            OfficialHoliday.objects.create(day=date(2026, 4, 23), name="Otro festivo")

        self.assertIn("day", raised.exception.message_dict)


class BoeNationalHolidaySyncServiceTests(TestCase):
    def test_extract_resolution_from_summary_returns_matching_resolution(self):
        service = BoeNationalHolidaySyncService()
        summary_payload = {
            "diario": [
                {
                    "seccion": [
                        {
                            "departamento": [
                                {
                                    "epigrafe": [
                                        {
                                            "item": [
                                                {
                                                    "identificador": "BOE-A-2025-21667",
                                                    "titulo": (
                                                        "Resolución de 17 de octubre de 2025, de la Dirección General"
                                                        " de Trabajo, por la que se publica la relación de fiestas"
                                                        " laborales para el año 2026."
                                                    ),
                                                    "url_html": "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2025-21667",
                                                }
                                            ]
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }

        result = service.extract_resolution_from_summary(
            summary_payload,
            service._normalize_text("relación de fiestas laborales para el año 2026"),
        )

        self.assertEqual(result.identifier, "BOE-A-2025-21667")
        self.assertIn("fiestas laborales para el año 2026", result.title.lower())

    def test_extract_national_holidays_filters_out_non_nationwide_rows(self):
        service = BoeNationalHolidaySyncService()
        resolution_html = """
        <table>
            <tr><th>Fecha de las fiestas</th><th>CCAA 1</th><th>CCAA 2</th></tr>
            <tr><td>Enero</td><td></td><td></td></tr>
            <tr><td>1 Año Nuevo.</td><td><abbr title="Fiesta nacional no sustituible">*</abbr></td><td><abbr title="Fiesta nacional no sustituible">*</abbr></td></tr>
            <tr><td>6 Epifanía del Señor.</td><td><abbr title="Fiesta Nacional respecto de la que no se ha ejercido la facultad de sustitución">**</abbr></td><td><abbr title="Fiesta Nacional respecto de la que no se ha ejercido la facultad de sustitución">**</abbr></td></tr>
            <tr><td>19 San José.</td><td><abbr title="Fiesta Nacional respecto de la que no se ha ejercido la facultad de sustitución">**</abbr></td><td></td></tr>
            <tr><td>Abril</td><td></td><td></td></tr>
            <tr><td>3 Viernes Santo.</td><td><abbr title="Fiesta nacional no sustituible">*</abbr></td><td><abbr title="Fiesta nacional no sustituible">*</abbr></td></tr>
            <tr><td>23 San Jorge.</td><td><abbr title="Fiesta de Comunidad Autónoma">***</abbr></td><td></td></tr>
        </table>
        """

        result = service.extract_national_holidays(2026, resolution_html)

        self.assertEqual(
            result,
            [
                OfficialHolidayImport(day=date(2026, 1, 1), name="Año Nuevo"),
                OfficialHolidayImport(day=date(2026, 1, 6), name="Epifanía del Señor"),
                OfficialHolidayImport(day=date(2026, 4, 3), name="Viernes Santo"),
            ],
        )


class DayAvailabilityResolverTests(TestCase):
    def setUp(self):
        super().setUp()
        self.agenda_settings = AgendaSettings.get_solo()

    def test_resolver_marks_a_regular_weekday_as_working_day(self):
        target_day = date(2026, 4, 8)

        result = DayAvailabilityResolver(
            agenda_settings=self.agenda_settings,
        ).resolve(target_day)

        self.assertEqual(result.status, DayAvailabilityResolver.WORKING_DAY)
        self.assertEqual(result.label, "Laborable")
        self.assertTrue(result.is_working_day)
        self.assertIsNone(result.manual_closure)
        self.assertIsNone(result.official_holiday)

    def test_resolver_marks_saturday_as_non_working_when_enabled(self):
        target_day = date(2026, 4, 11)

        result = DayAvailabilityResolver(
            agenda_settings=self.agenda_settings,
        ).resolve(target_day)

        self.assertEqual(result.status, DayAvailabilityResolver.NON_WORKING_SATURDAY)
        self.assertEqual(result.label, "Sabado no laborable")
        self.assertFalse(result.is_working_day)

    def test_resolver_marks_sunday_as_non_working_when_enabled(self):
        target_day = date(2026, 4, 12)

        result = DayAvailabilityResolver(
            agenda_settings=self.agenda_settings,
        ).resolve(target_day)

        self.assertEqual(result.status, DayAvailabilityResolver.NON_WORKING_SUNDAY)
        self.assertEqual(result.label, "Domingo no laborable")
        self.assertFalse(result.is_working_day)

    def test_resolver_can_leave_saturday_as_working_when_disabled(self):
        self.agenda_settings.saturdays_non_working = False
        self.agenda_settings.save()
        target_day = date(2026, 4, 11)

        result = DayAvailabilityResolver.resolve_for_global_agenda(target_day)

        self.assertEqual(result.status, DayAvailabilityResolver.WORKING_DAY)
        self.assertTrue(result.is_working_day)

    def test_resolver_marks_official_holiday_on_weekday_as_non_working(self):
        target_day = date(2026, 4, 23)
        official_holiday = OfficialHoliday.objects.create(day=target_day, name="San Jorge")

        result = DayAvailabilityResolver.resolve_for_global_agenda(target_day)

        self.assertEqual(result.status, DayAvailabilityResolver.OFFICIAL_HOLIDAY)
        self.assertEqual(result.label, "San Jorge")
        self.assertFalse(result.is_working_day)
        self.assertIsNone(result.manual_closure)
        self.assertEqual(result.official_holiday, official_holiday)

    def test_official_holiday_has_priority_over_non_working_weekend(self):
        target_day = date(2026, 4, 11)
        official_holiday = OfficialHoliday.objects.create(day=target_day, name="Festivo autonomico")

        result = DayAvailabilityResolver.resolve_for_global_agenda(target_day)

        self.assertEqual(result.status, DayAvailabilityResolver.OFFICIAL_HOLIDAY)
        self.assertEqual(result.label, "Festivo autonomico")
        self.assertFalse(result.is_working_day)
        self.assertEqual(result.official_holiday, official_holiday)

    def test_resolver_can_ignore_official_holiday_operatively_when_setting_disabled(self):
        self.agenda_settings.official_holidays_non_working = False
        self.agenda_settings.save(update_fields=["official_holidays_non_working"])
        target_day = date(2026, 4, 23)
        official_holiday = OfficialHoliday.objects.create(day=target_day, name="San Jorge")

        result = DayAvailabilityResolver.resolve_for_global_agenda(target_day)

        self.assertEqual(result.status, DayAvailabilityResolver.WORKING_DAY)
        self.assertEqual(result.label, "Laborable")
        self.assertTrue(result.is_working_day)
        self.assertEqual(result.official_holiday, official_holiday)

    def test_disabled_official_holiday_does_not_override_non_working_weekend(self):
        self.agenda_settings.official_holidays_non_working = False
        self.agenda_settings.save(update_fields=["official_holidays_non_working"])
        target_day = date(2026, 4, 11)
        official_holiday = OfficialHoliday.objects.create(day=target_day, name="Festivo autonomico")

        result = DayAvailabilityResolver.resolve_for_global_agenda(target_day)

        self.assertEqual(result.status, DayAvailabilityResolver.NON_WORKING_SATURDAY)
        self.assertEqual(result.label, "Sabado no laborable")
        self.assertFalse(result.is_working_day)
        self.assertEqual(result.official_holiday, official_holiday)

    def test_manual_closure_has_priority_over_non_working_weekend(self):
        target_day = date(2026, 4, 12)
        manual_closure = ManualClosure.objects.create(
            start_date=target_day,
            end_date=target_day,
            reason_type=ManualClosure.ReasonType.VACATION,
            label="Vacaciones de abril",
        )

        result = DayAvailabilityResolver.resolve_for_global_agenda(target_day)

        self.assertEqual(result.status, DayAvailabilityResolver.MANUAL_CLOSURE)
        self.assertEqual(result.label, "Vacaciones de abril")
        self.assertFalse(result.is_working_day)
        self.assertEqual(result.manual_closure, manual_closure)
        self.assertIsNone(result.official_holiday)

    def test_manual_closure_has_priority_over_official_holiday(self):
        target_day = date(2026, 4, 21)
        official_holiday = OfficialHoliday.objects.create(day=target_day, name="Festivo local")
        manual_closure = ManualClosure.objects.create(
            start_date=target_day,
            end_date=target_day,
            reason_type=ManualClosure.ReasonType.BUSINESS_CLOSURE,
            label="Cierre excepcional",
        )

        result = DayAvailabilityResolver.resolve_for_global_agenda(target_day)

        self.assertEqual(result.status, DayAvailabilityResolver.MANUAL_CLOSURE)
        self.assertEqual(result.label, "Cierre excepcional")
        self.assertFalse(result.is_working_day)
        self.assertEqual(result.manual_closure, manual_closure)
        self.assertIsNone(result.official_holiday)
        self.assertEqual(official_holiday.name, "Festivo local")

    def test_manual_closure_range_is_resolved_for_any_day_inside_the_range(self):
        ManualClosure.objects.create(
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 22),
            reason_type=ManualClosure.ReasonType.PERSONAL,
        )

        result = DayAvailabilityResolver.resolve_for_global_agenda(date(2026, 4, 21))

        self.assertEqual(result.status, DayAvailabilityResolver.MANUAL_CLOSURE)
        self.assertEqual(result.label, "Asunto personal")
        self.assertFalse(result.is_working_day)


class AgendaSettingsFormTests(TestCase):
    def test_form_updates_existing_singleton_instance(self):
        settings = AgendaSettings.get_solo()
        form = AgendaSettingsForm(
            data={
                "saturdays_non_working": "on",
                "official_holidays_non_working": "on",
            },
            instance=settings,
        )

        self.assertTrue(form.is_valid())
        saved_settings = form.save()

        self.assertTrue(saved_settings.saturdays_non_working)
        self.assertFalse(saved_settings.sundays_non_working)
        self.assertTrue(saved_settings.official_holidays_non_working)

    def test_form_can_disable_official_holiday_operational_effect(self):
        settings = AgendaSettings.get_solo()
        form = AgendaSettingsForm(
            data={
                "saturdays_non_working": "on",
                "sundays_non_working": "on",
            },
            instance=settings,
        )

        self.assertTrue(form.is_valid())
        saved_settings = form.save()

        self.assertFalse(saved_settings.official_holidays_non_working)


class BusinessSettingsFormTests(TestCase):
    def test_form_requires_all_business_fields(self):
        form = BusinessSettingsForm(data={})

        self.assertFalse(form.is_valid())
        self.assertEqual(
            set(form.errors.keys()),
            {"business_name", "phone", "email", "address", "city", "tax_id"},
        )

    def test_form_normalizes_basic_business_data(self):
        form = BusinessSettingsForm(
            data={
                "business_name": "  Centro   Atlas  ",
                "phone": " 600 123 123 ",
                "email": " INFO@ATLAS.COM ",
                "address": " Calle   Mayor  12 ",
                "city": " Madrid  Centro ",
                "tax_id": " b12345678 ",
            }
        )

        self.assertTrue(form.is_valid())
        business_settings = form.save()

        self.assertEqual(business_settings.business_name, "Centro Atlas")
        self.assertEqual(business_settings.phone, "600 123 123")
        self.assertEqual(business_settings.email, "info@atlas.com")
        self.assertEqual(business_settings.address, "Calle Mayor 12")
        self.assertEqual(business_settings.city, "Madrid Centro")
        self.assertEqual(business_settings.tax_id, "B12345678")


class ManualClosureFormTests(TestCase):
    def test_form_surfaces_overlapping_model_validation(self):
        ManualClosure.objects.create(
            start_date=date(2026, 4, 14),
            end_date=date(2026, 4, 16),
            reason_type=ManualClosure.ReasonType.BUSINESS_CLOSURE,
        )

        form = ManualClosureForm(
            data={
                "start_date": "2026-04-16",
                "end_date": "2026-04-18",
                "reason_type": ManualClosure.ReasonType.OTHER,
                "label": "",
                "notes": "",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("solapa", str(form.non_field_errors()))


class ServiceFormTests(TestCase):
    def test_form_exposes_only_operational_catalog_fields(self):
        form = ServiceForm(
            data={
                "name": "Primera consulta",
                "description": "Sesion inicial para valorar el caso.",
            }
        )

        self.assertTrue(form.is_valid())

        service = form.save()

        self.assertEqual(service.name, "Primera consulta")
        self.assertEqual(service.description, "Sesion inicial para valorar el caso.")
        self.assertTrue(service.is_active)
        self.assertEqual(service.color, "")
        self.assertEqual(list(form.fields), ["name", "description"])


class OfficialHolidaySyncFormTests(TestCase):
    def test_form_requires_a_valid_year(self):
        form = OfficialHolidaySyncForm(data={"year": ""})

        self.assertFalse(form.is_valid())
        self.assertIn("year", form.errors)


class AppAuthenticationBoundaryTests(AgendaBaseTestCase):
    def _assert_redirects_to_login(self, response, next_url):
        expected_url = f"{reverse('core:login')}?{urlencode({'next': next_url})}"
        self.assertRedirects(response, expected_url, fetch_redirect_response=False)

    def test_app_entrypoint_redirects_anonymous_user_to_app_login(self):
        today = self.operational_day
        query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        requested_url = f"{reverse('core:app_entrypoint')}?{query}"

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self._assert_redirects_to_login(response, requested_url)

    def test_appointment_create_redirects_anonymous_user_to_app_login(self):
        today = self.operational_day
        query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        requested_url = f"{reverse('core:appointment_create')}?{query}"

        response = self.client.get(
            reverse("core:appointment_create"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self._assert_redirects_to_login(response, requested_url)

    def test_client_create_redirects_anonymous_user_to_app_login(self):
        today = self.operational_day
        agenda_query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        appointment_next = f"{reverse('core:app_entrypoint')}?{agenda_query}"
        query = urlencode(
            {
                "year": today.year,
                "month": today.month,
                "day": today.day,
                "slot_time": "10:00",
                "appointment_next": appointment_next,
            }
        )
        requested_url = f"{reverse('core:client_create')}?{query}"

        response = self.client.get(
            reverse("core:client_create"),
            {
                "year": today.year,
                "month": today.month,
                "day": today.day,
                "slot_time": "10:00",
                "appointment_next": appointment_next,
            },
        )

        self._assert_redirects_to_login(response, requested_url)

    def test_client_create_from_list_redirects_anonymous_user_to_app_login(self):
        requested_url = f"{reverse('core:client_create')}?{urlencode({'next': reverse('core:client_list')})}"

        response = self.client.get(
            reverse("core:client_create"),
            {"next": reverse("core:client_list")},
        )

        self._assert_redirects_to_login(response, requested_url)

    def test_client_update_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:client_update", args=[self.primary_client.pk])

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_client_archive_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:client_archive", args=[self.primary_client.pk])

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_appointment_update_redirects_anonymous_user_to_app_login(self):
        today = self.operational_day
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        requested_url = f"{reverse('core:appointment_update', args=[appointment.pk])}?{query}"

        response = self.client.get(
            reverse("core:appointment_update", args=[appointment.pk]),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self._assert_redirects_to_login(response, requested_url)

    def test_client_detail_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:client_detail", args=[self.primary_client.pk])

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_client_list_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:client_list")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_archived_client_list_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:archived_client_list")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_client_reactivate_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:client_reactivate", args=[self.primary_client.pk])

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_client_delete_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:client_delete", args=[self.primary_client.pk])

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_ui_preview_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:ui_preview")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_calendar_ui_preview_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:calendar_ui_preview")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_agenda_settings_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:agenda_settings")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_settings_index_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:settings_index")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_business_settings_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:business_settings")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_service_settings_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:service_settings")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_service_create_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:service_create")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_service_update_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:service_update", args=[self.review_service.pk])

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_service_delete_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:service_delete", args=[self.review_service.pk])

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_manual_closure_create_redirects_anonymous_user_to_app_login(self):
        requested_url = reverse("core:manual_closure_create")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_manual_closure_update_redirects_anonymous_user_to_app_login(self):
        manual_closure = ManualClosure.objects.create(
            start_date=timezone.localdate(),
            end_date=timezone.localdate(),
            reason_type=ManualClosure.ReasonType.OTHER,
        )
        requested_url = reverse("core:manual_closure_update", args=[manual_closure.pk])

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_manual_closure_delete_redirects_anonymous_user_to_app_login(self):
        manual_closure = ManualClosure.objects.create(
            start_date=timezone.localdate(),
            end_date=timezone.localdate(),
            reason_type=ManualClosure.ReasonType.OTHER,
        )
        requested_url = reverse("core:manual_closure_delete", args=[manual_closure.pk])

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_ui_preview_returns_200_for_authenticated_user(self):
        self.login_app_user()

        response = self.client.get(reverse("core:ui_preview"))

        self.assertEqual(response.status_code, 200)

    def test_calendar_ui_preview_returns_200_for_authenticated_user(self):
        self.login_app_user()

        response = self.client.get(reverse("core:calendar_ui_preview"))

        self.assertEqual(response.status_code, 200)


class SessionAccessAndLoginBrandingTests(AgendaBaseTestCase):
    def test_login_route_is_exposed_at_acceso(self):
        self.assertEqual(reverse("core:login"), "/acceso/")

    def test_logout_route_is_exposed_at_salir(self):
        self.assertEqual(reverse("core:logout"), "/salir/")

    def test_login_page_uses_custom_branding_and_preserves_next(self):
        app_url = reverse("core:app_entrypoint")

        response = self.client.get(reverse("core:login"), {"next": app_url})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/login.html")
        self.assertTemplateNotUsed(response, "wagtailadmin/login.html")
        self.assertContains(response, "Agenda de Citas")
        self.assertContains(response, "ACCESO PROFESIONAL")
        self.assertContains(response, "Organiza tu agenda con más claridad")
        self.assertContains(response, "INICIO DE SESIÓN")
        self.assertContains(response, "Iniciar sesi")
        self.assertContains(response, "Agenda operativa interna")
        self.assertContains(response, "Agenda operativa")
        self.assertContains(response, "Calendario, clientes y ajustes internos del negocio.")
        self.assertContains(response, "Acceso - Agenda de Citas")
        self.assertContains(response, 'value="/app/"')
        self.assertContains(response, "Entrar")
        self.assertContains(response, "Usuario")
        self.assertContains(response, "Contrase")
        self.assertNotContains(response, "wagtail-login.css")
        self.assertNotContains(response, "Forgotten password")
        self.assertNotContains(response, "Remember me")
        self.assertNotContains(response, "Crear cuenta")
        self.assertNotContains(response, "Registrarse")

    def test_login_page_does_not_render_business_name_from_internal_settings(self):
        BusinessSettings.objects.create(
            business_name="Clinica Atlas",
            phone="600 123 123",
            email="info@atlas.test",
            address="Calle Mayor 12",
            city="Madrid",
            tax_id="B12345678",
        )

        response = self.client.get(reverse("core:login"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Clinica Atlas")
        self.assertContains(response, "Agenda de Citas")

    def test_authenticated_user_visiting_login_redirects_to_app_entrypoint(self):
        self.login_app_user()

        response = self.client.get(reverse("core:login"))

        self.assertRedirects(response, reverse("core:app_entrypoint"))

    def test_authenticated_user_visiting_login_redirects_to_safe_next(self):
        self.login_app_user()
        next_url = reverse("core:client_list")

        response = self.client.get(reverse("core:login"), {"next": next_url})

        self.assertRedirects(response, next_url)

    def test_authenticated_user_visiting_login_ignores_unsafe_next(self):
        self.login_app_user()

        response = self.client.get(reverse("core:login"), {"next": "https://example.com/"})

        self.assertRedirects(response, reverse("core:app_entrypoint"))

    def test_login_page_post_redirects_to_app_when_next_is_provided(self):
        response = self.client.post(
            reverse("core:login"),
            {
                "username": self.app_user.username,
                "password": "agenda-pass-123",
                "next": reverse("core:app_entrypoint"),
            },
        )

        self.assertRedirects(response, reverse("core:app_entrypoint"))
        self.assertIn("_auth_user_id", self.client.session)

    def test_login_page_post_redirects_to_app_when_next_is_not_safe(self):
        response = self.client.post(
            reverse("core:login"),
            {
                "username": self.app_user.username,
                "password": "agenda-pass-123",
                "next": "https://example.com/",
            },
        )

        self.assertRedirects(response, reverse("core:app_entrypoint"))
        self.assertIn("_auth_user_id", self.client.session)

    def test_authenticated_app_shell_shows_current_user_and_logout_action(self):
        self.login_app_user()

        response = self.client.get(reverse("core:app_entrypoint"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.app_user.username)
        self.assertContains(response, reverse("core:logout"))
        self.assertContains(response, "Cerrar sesion")
        self.assertContains(response, f'href="{reverse("core:app_entrypoint")}"')
        self.assertContains(response, f'href="{reverse("core:client_list")}"')
        self.assertContains(response, f'href="{reverse("core:settings_index")}"')
        self.assertContains(response, ">Agenda<")
        self.assertContains(response, ">Clientes<")
        self.assertContains(response, ">Ajustes<")
        self.assertContains(response, "Tu negocio")
        self.assertContains(response, "Agenda de Citas")
        self.assertNotContains(response, "/app/ui/")
        self.assertNotContains(response, "/app/calendar-ui/")
        self.assertNotContains(response, ">CMS<")

    def test_authenticated_app_shell_renders_business_name_when_configured(self):
        self.login_app_user()
        BusinessSettings.objects.create(
            business_name="Clinica Atlas",
            phone="600 123 123",
            email="info@atlas.test",
            address="Calle Mayor 12",
            city="Madrid",
            tax_id="B12345678",
        )

        response = self.client.get(reverse("core:app_entrypoint"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Clinica Atlas")
        self.assertContains(response, "Agenda de Citas")
        self.assertNotContains(response, "Tu negocio")

    def test_app_logout_redirects_to_login_and_closes_session(self):
        self.login_app_user()

        response = self.client.post(reverse("core:logout"))

        self.assertRedirects(response, reverse("core:login"), fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

        app_response = self.client.get(reverse("core:app_entrypoint"))
        expected_login_url = f"{reverse('core:login')}?{urlencode({'next': reverse('core:app_entrypoint')})}"
        self.assertRedirects(app_response, expected_login_url, fetch_redirect_response=False)

    def test_login_after_app_logout_returns_user_to_app_instead_of_admin(self):
        self.login_app_user()

        logout_response = self.client.post(reverse("core:logout"))
        expected_login_url = reverse("core:login")

        self.assertRedirects(logout_response, expected_login_url, fetch_redirect_response=False)

        login_page = self.client.get(logout_response.headers["Location"])
        self.assertEqual(login_page.status_code, 200)
        self.assertContains(login_page, 'value=""')

        login_response = self.client.post(
            reverse("core:login"),
            {
                "username": self.app_user.username,
                "password": "agenda-pass-123",
                "next": reverse("core:app_entrypoint"),
            },
        )

        self.assertRedirects(login_response, reverse("core:app_entrypoint"))


class AppointmentFlowViewTests(AuthenticatedAgendaBaseTestCase):
    def _slot_context(self, response, slot_time):
        for slot in response.context["agenda_timeline_slots"]:
            if slot["time"] == slot_time:
                return slot
        self.fail(f"Slot {slot_time} not found in agenda_timeline_slots.")

    def _appointment_form_data(self, **overrides):
        today = self.operational_day
        service_id = overrides.pop("service", self.review_service.pk)
        services = overrides.pop("services", [service_id])
        data = {
            "client": self.primary_client.pk,
            "services": services,
            "day": today.isoformat(),
            "slot_time": "11:00",
            "status": Appointment.Status.PENDING,
            "internal_notes": "Nota breve",
        }
        data.update(overrides)
        return data

    def test_create_view_can_create_a_valid_appointment(self):
        today = self.operational_day

        response = self.client.post(reverse("core:appointment_create"), self._appointment_form_data())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Appointment.objects.count(), 1)
        appointment = Appointment.objects.get()
        self.assertEqual(appointment.slot_day, today)
        self.assertEqual(appointment.slot_time, "11:00")
        self.assertEqual(self._appointment_service_ids(appointment), [self.review_service.pk])

    def test_create_view_can_create_a_valid_appointment_without_weekly_availability(self):
        today = self.operational_day

        response = self.client.post(reverse("core:appointment_create"), self._appointment_form_data())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Appointment.objects.count(), 1)
        appointment = Appointment.objects.get()
        self.assertEqual(appointment.slot_day, today)
        self.assertEqual(appointment.slot_time, "11:00")

    def test_create_view_can_create_appointment_with_multiple_services_without_service_duration_semantics(self):
        today = self.operational_day

        response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(
                services=[self.review_service.pk, self.control_service.pk],
            ),
        )

        self.assertEqual(response.status_code, 302)
        appointment = Appointment.objects.get()
        self.assertEqual(
            self._appointment_service_ids(appointment),
            [self.review_service.pk, self.control_service.pk],
        )
        self.assertEqual(appointment.end_at, agenda_end_at_for_slot(appointment.start_at))

        agenda_response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )
        slot_11 = self._slot_context(agenda_response, "11:00")
        self.assertEqual(slot_11["entries"][0]["service_label"], "Varios servicios")
        self.assertContains(agenda_response, "Varios servicios")

        history_response = self.client.get(reverse("core:client_detail", args=[self.primary_client.pk]))
        self.assertContains(history_response, "Varios servicios")

    def test_create_view_keeps_slot_based_validation_across_consecutive_slots(self):
        today = self.operational_day
        first_appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(
                client=self.secondary_client.pk,
                service=self.control_service.pk,
                slot_time="10:00",
            ),
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Appointment.objects.count(), 2)
        created_appointment = Appointment.objects.exclude(pk=first_appointment.pk).get()
        self.assertEqual(created_appointment.slot_time, "10:00")
        self.assertEqual(first_appointment.end_at, agenda_end_at_for_slot(first_appointment.start_at))
        self.assertEqual(created_appointment.end_at, agenda_end_at_for_slot(created_appointment.start_at))

    def test_create_view_uses_agenda_layout_structure_for_new_screen(self):
        today = self.operational_day
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.get(
            reverse("core:appointment_create"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="agenda-layout"')
        self.assertContains(response, 'class="agenda-month"')
        self.assertContains(response, 'class="agenda-panel appointment-create-panel"')
        self.assertContains(response, "Nueva cita")
        self.assertContains(response, "Elige un día y prepara la cita.")
        self.assertContains(response, "Tramos del día")
        self.assertContains(response, 'class="appointment-create-panel__form-fields"')
        self.assertContains(response, 'data-service-picker')
        self.assertContains(response, "Servicios")
        self.assertContains(response, 'data-service-picker-placeholder="Servicios"')
        self.assertContains(response, "/static/js/app.js?v=20260410-selector-fix")
        self.assertNotContains(response, "Selecciona servicios")
        self.assertContains(response, 'data-slot-picker')
        self.assertContains(response, 'type="hidden" name="day"')
        self.assertNotContains(response, "Formulario")
        self.assertNotContains(response, 'class="eyebrow agenda-header__eyebrow">Agenda operativa')
        self.assertNotContains(response, "Nueva cita · contexto de agenda")
        self.assertNotContains(response, "El tramo sigue indicandose desde el formulario.")
        self.assertNotContains(response, "hx-get=")
        self.assertFalse(response.context["calendar_hx_enabled"])
        self.assertTrue(response.context["calendar_interactive"])
        self.assertEqual(response.context["calendar_base_url"], reverse("core:appointment_create"))

    def test_create_view_marks_current_slot_as_selected_in_visual_list(self):
        today = self.operational_day

        response = self.client.get(
            reverse("core:appointment_create"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["slot_time"].value(), "09:00")
        self.assertRegex(
            response.content.decode(),
            r'class="[^"]*appointment-slot-row--selected[^"]*"[^>]*data-slot-value="09:00"[^>]*aria-pressed="true"',
        )

    def test_create_view_prefills_selected_day_and_slot_from_querystring(self):
        today = self.operational_day
        agenda_query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        next_url = f"{reverse('core:app_entrypoint')}?{agenda_query}"

        response = self.client.get(
            reverse("core:appointment_create"),
            {
                "year": today.year,
                "month": today.month,
                "day": today.day,
                "slot_time": "10:00",
                "next": next_url,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["day"].value(), today)
        self.assertEqual(response.context["form"]["slot_time"].value(), "10:00")
        self.assertEqual(response.context["back_url"], next_url)
        self.assertContains(response, 'value="10:00"')
        self.assertContains(response, 'name="next"')
        self.assertRegex(
            response.content.decode(),
            r'class="[^"]*appointment-slot-row--selected[^"]*"[^>]*data-slot-value="10:00"[^>]*aria-pressed="true"',
        )

    def test_create_view_offers_contextual_access_to_client_create(self):
        today = self.operational_day
        agenda_query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        next_url = f"{reverse('core:app_entrypoint')}?{agenda_query}"
        expected_client_create_url = (
            f"{reverse('core:client_create')}?"
            f"{urlencode({'year': today.year, 'month': today.month, 'day': today.day, 'slot_time': '10:00', 'appointment_next': next_url})}"
        )
        response = self.client.get(
            reverse("core:appointment_create"),
            {
                "year": today.year,
                "month": today.month,
                "day": today.day,
                "slot_time": "10:00",
                "next": next_url,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["client_create_url"], expected_client_create_url)
        self.assertContains(response, "Crear cliente nuevo")
        self.assertContains(response, 'data-client-create-link')
        self.assertContains(response, expected_client_create_url.replace("&", "&amp;"))

    def test_client_create_view_returns_200_and_keeps_contextual_back_url(self):
        today = self.operational_day
        agenda_query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        appointment_next = f"{reverse('core:app_entrypoint')}?{agenda_query}"
        expected_back_url = (
            f"{reverse('core:appointment_create')}?"
            f"{urlencode({'year': today.year, 'month': today.month, 'day': today.day, 'slot_time': '10:00', 'next': appointment_next})}"
        )

        response = self.client.get(
            reverse("core:client_create"),
            {
                "year": today.year,
                "month": today.month,
                "day": today.day,
                "slot_time": "10:00",
                "appointment_next": appointment_next,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/client_form.html")
        self.assertEqual(response.context["back_url"], expected_back_url)
        self.assertEqual(
            response.context["client_breadcrumbs"],
            [
                {"label": "Clientes", "url": reverse("core:client_list")},
                {"label": "Nuevo cliente", "url": ""},
            ],
        )
        self.assertIn("10:00", response.context["appointment_context_label"])
        self.assertIn(str(today.day), response.context["appointment_context_label"])
        self.assertContains(response, "Nuevo cliente")
        self.assertContains(response, "Volver a Nueva cita")
        self.assertContains(response, 'aria-label="Ruta de clientes"')
        self.assertContains(response, f'href="{reverse("core:client_list")}"')

    def test_client_create_view_creates_minimal_client_and_returns_to_new_appointment_preselected(self):
        today = self.operational_day
        agenda_query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        appointment_next = f"{reverse('core:app_entrypoint')}?{agenda_query}"

        response = self.client.post(
            reverse("core:client_create"),
            {
                "name": "Cliente Nuevo",
                "phone": "",
                "email": "",
                "notes": "",
                "year": today.year,
                "month": today.month,
                "day": today.day,
                "slot_time": "10:00",
                "appointment_next": appointment_next,
            },
        )

        created_client = Client.objects.get(name="Cliente Nuevo")
        expected_return_url = (
            f"{reverse('core:appointment_create')}?"
            f"{urlencode({'year': today.year, 'month': today.month, 'day': today.day, 'slot_time': '10:00', 'next': appointment_next, 'client': created_client.pk})}"
        )

        self.assertRedirects(response, expected_return_url, fetch_redirect_response=False)
        self.assertEqual(created_client.phone, "")
        self.assertEqual(created_client.email, "")
        self.assertEqual(created_client.notes, "")

        create_response = self.client.get(response.headers["Location"])

        self.assertEqual(create_response.status_code, 200)
        self.assertEqual(create_response.context["form"]["day"].value(), today)
        self.assertEqual(create_response.context["form"]["slot_time"].value(), "10:00")
        self.assertEqual(str(create_response.context["form"]["client"].value()), str(created_client.pk))
        self.assertEqual(create_response.context["back_url"], appointment_next)
        self.assertContains(create_response, "Cliente Nuevo")

    def test_client_create_view_from_list_uses_client_list_as_natural_back_url(self):
        list_url = reverse("core:client_list")

        response = self.client.get(
            reverse("core:client_create"),
            {"next": list_url},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/client_form.html")
        self.assertEqual(response.context["back_url"], list_url)
        self.assertEqual(response.context["next_url"], list_url)
        self.assertEqual(response.context["appointment_context_label"], "")
        self.assertEqual(
            response.context["client_breadcrumbs"],
            [
                {"label": "Clientes", "url": reverse("core:client_list")},
                {"label": "Nuevo cliente", "url": ""},
            ],
        )
        self.assertContains(response, "Nuevo cliente")
        self.assertContains(response, "Volver a clientes")
        self.assertNotContains(response, "Nueva cita en curso")

    def test_client_create_view_from_list_creates_client_and_redirects_to_detail_with_back_to_list(self):
        list_url = reverse("core:client_list")

        response = self.client.post(
            reverse("core:client_create"),
            {
                "name": "Cliente de listado",
                "phone": "+34 600 333 333",
                "email": "listado@example.com",
                "notes": "Alta manual desde clientes",
                "next": list_url,
            },
        )

        created_client = Client.objects.get(name="Cliente de listado")
        expected_redirect = (
            f"{reverse('core:client_detail', args=[created_client.pk])}"
            f"?{urlencode({'next': list_url})}"
        )

        self.assertRedirects(response, expected_redirect, fetch_redirect_response=False)
        detail_response = self.client.get(response.headers["Location"])

        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(detail_response.context["back_url"], list_url)
        self.assertContains(detail_response, "Cliente de listado")

    def test_create_view_prefills_client_from_querystring_and_uses_client_detail_back_url(self):
        next_url = (
            f"{reverse('core:client_detail', args=[self.primary_client.pk])}"
            f"?{urlencode({'next': reverse('core:app_entrypoint')})}"
        )

        response = self.client.get(
            reverse("core:appointment_create"),
            {
                "client": self.primary_client.pk,
                "next": next_url,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.context["form"]["client"].value()), str(self.primary_client.pk))
        self.assertEqual(response.context["back_url"], next_url)
        self.assertEqual(response.context["back_label"], "Volver a ficha")
        self.assertContains(response, "Volver a ficha")
        self.assertContains(response, 'name="next"')

    def test_create_view_prefills_client_from_client_list_and_uses_client_list_back_url(self):
        list_url = reverse("core:client_list")

        response = self.client.get(
            reverse("core:appointment_create"),
            {
                "client": self.primary_client.pk,
                "next": list_url,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.context["form"]["client"].value()), str(self.primary_client.pk))
        self.assertEqual(response.context["back_url"], list_url)
        self.assertEqual(response.context["back_label"], "Volver a clientes")
        self.assertContains(response, "Volver a clientes")
        self.assertContains(response, 'name="next"')

    def test_create_view_prefills_client_and_active_services_from_source_appointment(self):
        source_appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            self.operational_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        source_appointment.services.set([self.review_service, self.control_service])
        source_appointment.internal_notes = "Nota de origen"
        source_appointment.save(update_fields=["internal_notes"])
        next_url = reverse("core:client_detail", args=[self.primary_client.pk])

        response = self.client.get(
            reverse("core:appointment_create"),
            {
                "source_appointment": source_appointment.pk,
                "next": next_url,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.context["form"]["client"].value()), str(self.primary_client.pk))
        self.assertCountEqual(
            response.context["form"]["services"].value(),
            [self.review_service.pk, self.control_service.pk],
        )
        self.assertEqual(response.context["form"]["status"].value(), Appointment.Status.PENDING)
        self.assertEqual(response.context["back_url"], next_url)
        self.assertNotContains(response, "Nota de origen")

    def test_create_view_reuses_source_appointment_services_only_when_still_selectable(self):
        source_appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            self.operational_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        source_appointment.services.set([self.review_service, self.control_service])
        self.control_service.is_active = False
        self.control_service.save(update_fields=["is_active"])

        response = self.client.get(
            reverse("core:appointment_create"),
            {
                "source_appointment": source_appointment.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["services"].value(), [self.review_service.pk])
        self.assertContains(response, self.review_service.name)
        self.assertNotContains(response, self.control_service.name)

    def test_create_view_does_not_prefill_archived_client_even_if_source_appointment_is_provided(self):
        source_appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            self.operational_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self.primary_client.is_archived = True
        self.primary_client.save(update_fields=["is_archived"])

        response = self.client.get(
            reverse("core:appointment_create"),
            {
                "source_appointment": source_appointment.pk,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["form"]["client"].value())
        self.assertEqual(response.context["form"]["services"].value(), [self.review_service.pk])
        self.assertNotContains(response, self.primary_client.name)

    def test_create_view_can_create_new_appointment_from_source_appointment_prefill(self):
        source_appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            self.operational_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        source_appointment.services.set([self.review_service, self.control_service])
        target_day = self._next_working_day(start_day=self.operational_day)

        response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(
                day=target_day.isoformat(),
                slot_time="11:00",
                services=[self.review_service.pk, self.control_service.pk],
                status=Appointment.Status.CONFIRMED,
                internal_notes="Nueva nota",
            ),
        )

        self.assertEqual(response.status_code, 302)
        created_appointment = Appointment.objects.exclude(pk=source_appointment.pk).get()
        self.assertEqual(created_appointment.client_id, self.primary_client.pk)
        self.assertEqual(
            self._appointment_service_ids(created_appointment),
            [self.review_service.pk, self.control_service.pk],
        )
        self.assertEqual(created_appointment.slot_day, target_day)
        self.assertEqual(created_appointment.slot_time, "11:00")
        self.assertEqual(created_appointment.status, Appointment.Status.CONFIRMED)
        self.assertEqual(created_appointment.internal_notes, "Nueva nota")

    def test_archived_client_is_hidden_for_new_appointments_but_kept_for_existing_edits(self):
        appointment = self._create_appointment(
            self.primary_client,
            self.control_service,
            self.operational_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self.primary_client.is_archived = True
        self.primary_client.save(update_fields=["is_archived"])

        create_response = self.client.get(reverse("core:appointment_create"))
        edit_form = AppointmentForm(instance=appointment)

        self.assertEqual(create_response.status_code, 200)
        self.assertNotIn(self.primary_client, list(create_response.context["form"].fields["client"].queryset))
        self.assertIn(self.primary_client, list(edit_form.fields["client"].queryset))
        self.assertNotContains(create_response, self.primary_client.name)

    def test_create_view_does_not_offer_cancelled_status(self):
        today = self.operational_day

        response = self.client.get(
            reverse("core:appointment_create"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [value for value, _label in response.context["form"].fields["status"].choices],
            [Appointment.Status.PENDING, Appointment.Status.CONFIRMED],
        )
        self.assertNotContains(response, 'value="cancelled"')

    def test_create_view_rejects_manipulated_cancelled_status(self):
        today = self.operational_day

        response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(status=Appointment.Status.CANCELLED),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Appointment.objects.count(), 0)
        self.assertIn("status", response.context["form"].errors)
        self.assertContains(response, "cancelled is not one of the available choices")

    def test_create_view_renders_non_bookable_slots_as_inert_and_keeps_bound_selection_on_invalid_post(self):
        today = self.operational_day
        self._create_block(today, "10:00", label="Bloqueo interno")
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )
        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )

        get_response = self.client.get(
            reverse("core:appointment_create"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        get_content = get_response.content.decode()
        self.assertRegex(
            get_content,
            r'class="[^"]*appointment-slot-row--inactive[^"]*"[^>]*data-slot-value="10:00"[^>]*aria-disabled="true"',
        )
        self.assertRegex(
            get_content,
            r'class="[^"]*appointment-slot-row--inactive[^"]*appointment-slot-row--complete[^"]*"[^>]*data-slot-value="11:00"[^>]*aria-disabled="true"',
        )
        self.assertNotRegex(get_content, r'data-slot-button[^>]*data-slot-value="10:00"')
        self.assertNotRegex(get_content, r'data-slot-button[^>]*data-slot-value="11:00"')

        invalid_response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(client=self.secondary_client.pk),
        )

        invalid_content = invalid_response.content.decode()
        self.assertEqual(invalid_response.status_code, 200)
        self.assertEqual(invalid_response.context["form"]["slot_time"].value(), "11:00")
        self.assertContains(invalid_response, "capacidad maxima")
        self.assertRegex(
            invalid_content,
            r'class="[^"]*appointment-slot-row--inactive[^"]*appointment-slot-row--complete[^"]*appointment-slot-row--selected[^"]*"[^>]*data-slot-value="11:00"[^>]*aria-disabled="true"',
        )

    def test_create_view_keeps_next_back_url_on_invalid_post(self):
        today = self.operational_day
        agenda_query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        next_url = f"{reverse('core:app_entrypoint')}?{agenda_query}"
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(9, 0),
            Appointment.Status.PENDING,
        )
        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(
                client=self.secondary_client.pk,
                day=today.isoformat(),
                slot_time="09:00",
                next=next_url,
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "capacidad maxima")
        self.assertEqual(response.context["back_url"], next_url)
        self.assertContains(response, 'name="next"')

    def test_create_view_rejects_blocked_slot(self):
        today = self.operational_day
        self._create_block(today, "11:00", label="Bloqueo interno")

        response = self.client.post(reverse("core:appointment_create"), self._appointment_form_data())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bloqueado")
        self.assertEqual(Appointment.objects.count(), 0)

    def test_create_view_uses_base_capacity_without_weekly_availability(self):
        today = self.operational_day
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )

        third_response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(client=self.tertiary_client.pk),
        )
        fourth_response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(client=self.fourth_client.pk),
        )

        self.assertEqual(third_response.status_code, 302)
        self.assertEqual(fourth_response.status_code, 200)
        self.assertContains(fourth_response, "capacidad maxima")
        self.assertEqual(Appointment.active_slot_appointments_count(today, "11:00"), 3)

    def test_create_view_rejects_complete_slot(self):
        today = self.operational_day
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )
        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(client=self.fourth_client.pk),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "capacidad maxima")
        self.assertEqual(Appointment.objects.count(), 3)

    def test_create_view_rejects_non_working_saturday(self):
        selected_day = self._next_weekday(5)

        response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(
                day=selected_day.isoformat(),
                slot_time="11:00",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Appointment.objects.count(), 0)
        self.assertIn("slot_time", response.context["form"].errors)
        self.assertContains(response, "La agenda no opera el dia seleccionado")
        self.assertContains(response, "Sabado no laborable")

    def test_create_view_rejects_non_working_sunday(self):
        selected_day = self._next_weekday(6)

        response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(
                day=selected_day.isoformat(),
                slot_time="11:00",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Appointment.objects.count(), 0)
        self.assertIn("slot_time", response.context["form"].errors)
        self.assertContains(response, "La agenda no opera el dia seleccionado")
        self.assertContains(response, "Domingo no laborable")

    def test_create_view_rejects_manual_closure(self):
        selected_day = self._next_weekday(2)
        ManualClosure.objects.create(
            start_date=selected_day,
            end_date=selected_day,
            reason_type=ManualClosure.ReasonType.VACATION,
            label="Vacaciones de abril",
        )

        response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(
                day=selected_day.isoformat(),
                slot_time="11:00",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Appointment.objects.count(), 0)
        self.assertIn("slot_time", response.context["form"].errors)
        self.assertContains(response, "La agenda no opera el dia seleccionado")
        self.assertContains(response, "Vacaciones de abril")

    def test_create_view_accepts_third_appointment_when_capacity_is_three_and_rejects_fourth(self):
        today = self.operational_day
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )

        third_response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(client=self.tertiary_client.pk),
        )

        self.assertEqual(third_response.status_code, 302)
        self.assertEqual(Appointment.active_slot_appointments_count(today, "11:00"), 3)

        fourth_response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(client=self.fourth_client.pk),
        )

        self.assertEqual(fourth_response.status_code, 200)
        self.assertContains(fourth_response, "capacidad maxima")
        self.assertEqual(Appointment.active_slot_appointments_count(today, "11:00"), 3)

    def test_create_view_uses_same_capacity_rule_and_disables_complete_slot(self):
        today = self.operational_day
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )

        almost_full_response = self.client.get(
            reverse("core:appointment_create"),
            {"year": today.year, "month": today.month, "day": today.day},
        )
        slot_11 = self._slot_context(almost_full_response, "11:00")
        self.assertEqual(slot_11["complete_label"], "")
        self.assertEqual(slot_11["busy_label"], "2/3 ocupadas")
        self.assertContains(almost_full_response, "11:00 · 2/3 ocupadas")

        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )

        full_response = self.client.get(
            reverse("core:appointment_create"),
            {"year": today.year, "month": today.month, "day": today.day},
        )
        slot_11 = self._slot_context(full_response, "11:00")

        self.assertEqual(slot_11["complete_label"], "Completo")
        self.assertRegex(
            full_response.content.decode(),
            r'class="[^"]*appointment-slot-row--inactive[^"]*appointment-slot-row--complete[^"]*"[^>]*data-slot-value="11:00"[^>]*aria-disabled="true"',
        )
        self.assertContains(full_response, "11:00 · Completo")
        self.assertContains(full_response, 'value="11:00" disabled')
        self.assertContains(full_response, 'class="field__feedback field__feedback--slot"')
        self.assertContains(full_response, "Solo se pueden elegir tramos con plaza libre dentro de su capacidad.")

    def test_update_view_uses_same_shell_with_contextual_calendar_and_native_date_and_slot_fields(self):
        today = self.operational_day
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.get(
            reverse("core:appointment_update", args=[appointment.pk]),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="agenda-layout"')
        self.assertContains(response, 'class="agenda-month agenda-month--contextual"')
        self.assertContains(response, 'class="agenda-panel appointment-create-panel appointment-edit-panel"')
        self.assertContains(response, "Editar cita")
        self.assertContains(response, "Guardar cambios")
        self.assertContains(response, 'type="date"')
        self.assertContains(response, 'name="day"')
        self.assertContains(response, 'name="slot_time"')
        self.assertContains(response, 'class="agenda-month__nav-link agenda-month__nav-link--static"')
        self.assertContains(response, 'class="agenda-day__link agenda-day__link--static"')
        self.assertNotContains(response, 'type="hidden" name="day"')
        self.assertNotContains(response, 'data-slot-picker')
        self.assertNotContains(response, "hx-get=")
        self.assertFalse(response.context["calendar_hx_enabled"])
        self.assertFalse(response.context["calendar_interactive"])
        self.assertEqual(response.context["form"]["day"].value(), today)
        self.assertEqual(response.context["form"]["slot_time"].value(), "09:00")
        self.assertContains(response, "Eliminar cita")
        self.assertContains(response, 'data-delete-mode-input')
        self.assertContains(response, 'data-delete-trigger')
        self.assertContains(response, 'data-cancel-notice')
        self.assertContains(response, 'data-delete-confirmation')

    def test_update_view_shows_client_link_with_next_to_current_context(self):
        today = self.operational_day
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        current_path = (
            f"{reverse('core:appointment_update', args=[appointment.pk])}"
            f"?{urlencode({'year': today.year, 'month': today.month, 'day': today.day})}"
        )
        expected_client_url = (
            f"{reverse('core:client_detail', args=[appointment.client_id])}"
            f"?{urlencode({'next': current_path})}"
        )

        response = self.client.get(
            reverse("core:appointment_update", args=[appointment.pk]),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ver cliente")
        self.assertContains(response, expected_client_url)

    def test_update_view_keeps_contextual_calendar_tied_to_appointment_day_on_get(self):
        today = self.operational_day
        query_day = today + timedelta(days=1)
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.get(
            reverse("core:appointment_update", args=[appointment.pk]),
            {"year": query_day.year, "month": query_day.month, "day": query_day.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_day_iso"], today.isoformat())
        self.assertEqual(response.context["form"]["day"].value(), today)
        self.assertEqual(
            response.context["back_url"],
            f"{reverse('core:app_entrypoint')}?{urlencode({'year': today.year, 'month': today.month, 'day': today.day})}",
        )

    def test_update_view_keeps_validation_and_allows_valid_edit(self):
        today = self.operational_day
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(10, 0),
            Appointment.Status.PENDING,
        )
        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(10, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.fourth_client,
            self.control_service,
            today,
            time(10, 0),
            Appointment.Status.PENDING,
        )

        blocked_response = self.client.post(
            reverse("core:appointment_update", args=[appointment.pk]),
            self._appointment_form_data(
                client=appointment.client_id,
                services=self._appointment_service_ids(appointment),
                day=today.isoformat(),
                slot_time="10:00",
                status=appointment.status,
                internal_notes="Intento en tramo completo",
            ),
        )

        self.assertEqual(blocked_response.status_code, 200)
        self.assertContains(blocked_response, "capacidad maxima")

        valid_response = self.client.post(
            reverse("core:appointment_update", args=[appointment.pk]),
            self._appointment_form_data(
                client=appointment.client_id,
                service=self.control_service.pk,
                day=today.isoformat(),
                slot_time="11:00",
                status=Appointment.Status.PENDING,
                internal_notes="Cambio valido",
            ),
        )

        self.assertEqual(valid_response.status_code, 302)
        self.assertRedirects(
            valid_response,
            f"{reverse('core:app_entrypoint')}?{urlencode({'year': today.year, 'month': today.month, 'day': today.day})}",
            fetch_redirect_response=False,
        )
        appointment.refresh_from_db()
        self.assertEqual(appointment.slot_time, "11:00")
        self.assertEqual(appointment.status, Appointment.Status.PENDING)
        self.assertEqual(appointment.internal_notes, "Cambio valido")
        self.assertEqual(self._appointment_service_ids(appointment), [self.control_service.pk])

    def test_update_view_can_store_multiple_services_without_service_duration_semantics(self):
        today = self.operational_day
        appointment = self._create_appointment(
            self.primary_client,
            self.control_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.post(
            reverse("core:appointment_update", args=[appointment.pk]),
            self._appointment_form_data(
                client=appointment.client_id,
                services=[self.review_service.pk, self.control_service.pk],
                day=today.isoformat(),
                slot_time="11:00",
                status=Appointment.Status.PENDING,
                internal_notes="Dos servicios",
            ),
        )

        self.assertEqual(response.status_code, 302)
        appointment.refresh_from_db()
        self.assertEqual(
            self._appointment_service_ids(appointment),
            [self.review_service.pk, self.control_service.pk],
        )
        self.assertEqual(appointment.end_at, agenda_end_at_for_slot(appointment.start_at))

    def test_update_view_rejects_move_to_non_operational_day(self):
        working_day = self._next_weekday(2)
        closed_day = self._next_weekday(5, start_day=working_day)
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            working_day,
            time(11, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.post(
            reverse("core:appointment_update", args=[appointment.pk]),
            self._appointment_form_data(
                client=appointment.client_id,
                services=self._appointment_service_ids(appointment),
                day=closed_day.isoformat(),
                slot_time="11:00",
                status=appointment.status,
                internal_notes="Intento en sabado cerrado",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("slot_time", response.context["form"].errors)
        self.assertContains(response, "La agenda no opera el dia seleccionado")
        self.assertContains(response, "Sabado no laborable")
        appointment.refresh_from_db()
        self.assertEqual(appointment.slot_day, working_day)
        self.assertEqual(appointment.slot_time, "11:00")

    def test_update_view_returns_404_for_cancelled_appointment(self):
        today = self.operational_day
        appointment = self._create_existing_cancelled_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
        )

        response = self.client.get(reverse("core:appointment_update", args=[appointment.pk]))

        self.assertEqual(response.status_code, 404)

    def test_update_view_can_cancel_appointment_without_deleting_and_free_slot_for_new_booking(self):
        today = self.operational_day
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        cancel_response = self.client.post(
            reverse("core:appointment_update", args=[appointment.pk]),
            self._appointment_form_data(
                client=appointment.client_id,
                services=self._appointment_service_ids(appointment),
                day=today.isoformat(),
                slot_time="09:00",
                status=Appointment.Status.CANCELLED,
                internal_notes="Cancelada sin borrar",
                delete_mode="false",
            ),
        )

        self.assertEqual(cancel_response.status_code, 302)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CANCELLED)
        self.assertEqual(Appointment.objects.filter(pk=appointment.pk).count(), 1)
        self.assertEqual(Appointment.active_slot_appointments_count(today, "09:00"), 0)

        agenda_response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )
        slot_09 = self._slot_context(agenda_response, "09:00")

        self.assertEqual(agenda_response.status_code, 200)
        self.assertEqual(slot_09["available_label"], "Disponible")
        self.assertEqual(slot_09["entries"], [])
        self.assertNotContains(agenda_response, self.primary_client.name)

        replacement_response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(
                client=self.secondary_client.pk,
                slot_time="09:00",
            ),
        )

        self.assertEqual(replacement_response.status_code, 302)
        self.assertEqual(Appointment.active_slot_appointments_count(today, "09:00"), 1)
        self.assertEqual(Appointment.objects.filter(status=Appointment.Status.CANCELLED).count(), 1)

    def test_update_view_keeps_cancelled_appointment_in_client_history(self):
        today = self.operational_day
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        cancel_response = self.client.post(
            reverse("core:appointment_update", args=[appointment.pk]),
            self._appointment_form_data(
                client=appointment.client_id,
                services=self._appointment_service_ids(appointment),
                day=today.isoformat(),
                slot_time="09:00",
                status=Appointment.Status.CANCELLED,
                internal_notes="Cancelada y mantenida en historial",
                delete_mode="false",
            ),
        )

        self.assertEqual(cancel_response.status_code, 302)

        history_response = self.client.get(
            reverse("core:client_detail", args=[appointment.client_id]),
            {"next": reverse("core:app_entrypoint")},
        )

        self.assertEqual(history_response.status_code, 200)
        self.assertContains(history_response, "Cancelada")
        self.assertContains(history_response, self.review_service.name)
        self.assertNotContains(history_response, reverse("core:appointment_update", args=[appointment.pk]))
        self.assertContains(history_response, "Sin accion operativa")

    def test_update_view_rejects_post_for_cancelled_appointment(self):
        today = self.operational_day
        appointment = self._create_existing_cancelled_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
        )

        response = self.client.post(
            reverse("core:appointment_update", args=[appointment.pk]),
            self._appointment_form_data(
                client=appointment.client_id,
                services=self._appointment_service_ids(appointment),
                day=today.isoformat(),
                slot_time="09:00",
                status=Appointment.Status.CANCELLED,
                internal_notes="Intento de editar cita ya cancelada",
                delete_mode="false",
            ),
        )

        self.assertEqual(response.status_code, 404)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CANCELLED)
        self.assertEqual(appointment.slot_time, "09:00")

    def test_update_view_requires_explicit_delete_confirmation_and_then_removes_appointment(self):
        today = self.operational_day
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        confirmation_response = self.client.post(
            reverse("core:appointment_update", args=[appointment.pk]),
            self._appointment_form_data(
                client=appointment.client_id,
                services=self._appointment_service_ids(appointment),
                day=today.isoformat(),
                slot_time="09:00",
                status=appointment.status,
                internal_notes="Preparando borrado",
                delete_mode="false",
                edit_intent="show_delete_confirmation",
            ),
        )

        self.assertEqual(confirmation_response.status_code, 200)
        self.assertContains(confirmation_response, "Confirmar eliminacion")
        self.assertContains(confirmation_response, "desaparecera tambien del historial")
        self.assertRegex(confirmation_response.content.decode(), r'data-delete-confirmation\s*>')
        self.assertRegex(confirmation_response.content.decode(), r'data-cancel-notice[^>]*hidden')
        self.assertEqual(Appointment.objects.filter(pk=appointment.pk).count(), 1)

        delete_response = self.client.post(
            reverse("core:appointment_update", args=[appointment.pk]),
            self._appointment_form_data(
                client=appointment.client_id,
                services=self._appointment_service_ids(appointment),
                day=today.isoformat(),
                slot_time="09:00",
                status=appointment.status,
                internal_notes="Borrado confirmado",
                delete_mode="true",
                edit_intent="confirm_delete",
            ),
        )

        self.assertEqual(delete_response.status_code, 302)
        self.assertFalse(Appointment.objects.filter(pk=appointment.pk).exists())
        self.assertEqual(Appointment.active_slot_appointments_count(today, "09:00"), 0)

        replacement_response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(
                client=self.secondary_client.pk,
                slot_time="09:00",
            ),
        )

        self.assertEqual(replacement_response.status_code, 302)
        self.assertEqual(Appointment.objects.filter(status=Appointment.Status.CANCELLED).count(), 0)


class AvailabilityBlockToggleViewTests(AuthenticatedAgendaBaseTestCase):
    def _slot_context(self, response, slot_time):
        for slot in response.context["agenda_timeline_slots"]:
            if slot["time"] == slot_time:
                return slot
        self.fail(f"Slot {slot_time} not found in agenda_timeline_slots.")

    def _toggle_block(self, target_day, slot_time):
        return self.client.post(
            reverse("core:availability_block_toggle"),
            {
                "day": target_day.isoformat(),
                "slot_time": slot_time,
            },
        )

    def test_toggle_view_creates_valid_block_on_free_available_slot(self):
        selected_day = self._next_weekday(4)
        expected_redirect = (
            f"{reverse('core:app_entrypoint')}?"
            f"{urlencode({'year': selected_day.year, 'month': selected_day.month, 'day': selected_day.day})}"
        )

        response = self._toggle_block(selected_day, "10:00")

        self.assertRedirects(response, expected_redirect, fetch_redirect_response=False)
        self.assertTrue(
            AvailabilityBlock.objects.filter(
                day=selected_day,
                slot_time="10:00",
                label="Bloqueo puntual",
            ).exists()
        )

        agenda_response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )
        slot_10 = self._slot_context(agenda_response, "10:00")
        self.assertEqual(slot_10["blocked_label"], "Bloqueo puntual")
        self.assertEqual(slot_10["entries"], [])

    def test_toggle_view_removes_existing_block(self):
        selected_day = self._next_weekday(0)
        expected_redirect = (
            f"{reverse('core:app_entrypoint')}?"
            f"{urlencode({'year': selected_day.year, 'month': selected_day.month, 'day': selected_day.day})}"
        )
        self._create_block(selected_day, "10:00", label="Bloqueo puntual")

        response = self._toggle_block(selected_day, "10:00")

        self.assertRedirects(response, expected_redirect, fetch_redirect_response=False)
        self.assertFalse(AvailabilityBlock.objects.filter(day=selected_day, slot_time="10:00").exists())

        agenda_response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )
        slot_10 = self._slot_context(agenda_response, "10:00")
        self.assertEqual(slot_10["blocked_label"], "")
        self.assertEqual(slot_10["available_label"], "Disponible")

    def test_toggle_view_rejects_block_creation_for_occupied_slot(self):
        selected_day = self._next_weekday(1)
        expected_redirect = (
            f"{reverse('core:app_entrypoint')}?"
            f"{urlencode({'year': selected_day.year, 'month': selected_day.month, 'day': selected_day.day})}"
        )
        self._create_appointment(
            self.primary_client,
            self.review_service,
            selected_day,
            time(10, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self._toggle_block(selected_day, "10:00")

        self.assertRedirects(response, expected_redirect, fetch_redirect_response=False)
        self.assertFalse(AvailabilityBlock.objects.filter(day=selected_day, slot_time="10:00").exists())

        agenda_response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )
        slot_10 = self._slot_context(agenda_response, "10:00")
        self.assertEqual(slot_10["blocked_label"], "")
        self.assertEqual(len(slot_10["entries"]), 1)

    def test_toggle_view_creates_block_on_default_operational_slot_without_weekly_availability(self):
        selected_day = self._next_weekday(2)
        expected_redirect = (
            f"{reverse('core:app_entrypoint')}?"
            f"{urlencode({'year': selected_day.year, 'month': selected_day.month, 'day': selected_day.day})}"
        )

        response = self._toggle_block(selected_day, "10:00")

        self.assertRedirects(response, expected_redirect, fetch_redirect_response=False)
        self.assertTrue(AvailabilityBlock.objects.filter(day=selected_day, slot_time="10:00").exists())

        agenda_response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )
        slot_10 = self._slot_context(agenda_response, "10:00")
        self.assertEqual(slot_10["blocked_label"], "Bloqueo puntual")
        self.assertEqual(slot_10["unavailable_label"], "")

    def test_toggle_view_preserves_selected_day_context_after_action(self):
        selected_day = self._next_weekday(3)
        expected_redirect = (
            f"{reverse('core:app_entrypoint')}?"
            f"{urlencode({'year': selected_day.year, 'month': selected_day.month, 'day': selected_day.day})}"
        )

        response = self._toggle_block(selected_day, "11:00")

        self.assertRedirects(response, expected_redirect, fetch_redirect_response=False)

    def test_toggle_view_rejects_non_working_saturday(self):
        selected_day = self._next_weekday(5)
        expected_redirect = (
            f"{reverse('core:app_entrypoint')}?"
            f"{urlencode({'year': selected_day.year, 'month': selected_day.month, 'day': selected_day.day})}"
        )

        response = self._toggle_block(selected_day, "10:00")

        self.assertRedirects(response, expected_redirect, fetch_redirect_response=False)
        self.assertFalse(AvailabilityBlock.objects.filter(day=selected_day, slot_time="10:00").exists())

    def test_toggle_view_rejects_non_working_sunday(self):
        selected_day = self._next_weekday(6)
        expected_redirect = (
            f"{reverse('core:app_entrypoint')}?"
            f"{urlencode({'year': selected_day.year, 'month': selected_day.month, 'day': selected_day.day})}"
        )

        response = self._toggle_block(selected_day, "10:00")

        self.assertRedirects(response, expected_redirect, fetch_redirect_response=False)
        self.assertFalse(AvailabilityBlock.objects.filter(day=selected_day, slot_time="10:00").exists())

    def test_toggle_view_rejects_manual_closure(self):
        selected_day = self._next_weekday(2)
        expected_redirect = (
            f"{reverse('core:app_entrypoint')}?"
            f"{urlencode({'year': selected_day.year, 'month': selected_day.month, 'day': selected_day.day})}"
        )
        ManualClosure.objects.create(
            start_date=selected_day,
            end_date=selected_day,
            reason_type=ManualClosure.ReasonType.LOCAL_HOLIDAY,
            label="Cierre especial",
        )

        response = self._toggle_block(selected_day, "10:00")

        self.assertRedirects(response, expected_redirect, fetch_redirect_response=False)
        self.assertFalse(AvailabilityBlock.objects.filter(day=selected_day, slot_time="10:00").exists())

    def test_toggle_view_keeps_existing_block_when_day_becomes_non_operational(self):
        selected_day = self._next_weekday(2)
        expected_redirect = (
            f"{reverse('core:app_entrypoint')}?"
            f"{urlencode({'year': selected_day.year, 'month': selected_day.month, 'day': selected_day.day})}"
        )
        self._create_block(selected_day, "10:00", label="Bloqueo puntual")
        ManualClosure.objects.create(
            start_date=selected_day,
            end_date=selected_day,
            reason_type=ManualClosure.ReasonType.LOCAL_HOLIDAY,
            label="Cierre especial",
        )

        response = self._toggle_block(selected_day, "10:00")

        self.assertRedirects(response, expected_redirect, fetch_redirect_response=False)
        self.assertTrue(AvailabilityBlock.objects.filter(day=selected_day, slot_time="10:00").exists())


class ClientDetailViewTests(AuthenticatedAgendaBaseTestCase):
    def _client_detail_url(self, client, next_url=None):
        base_url = reverse("core:client_detail", args=[client.pk])
        if not next_url:
            return base_url
        return f"{base_url}?{urlencode({'next': next_url})}"

    def test_client_detail_view_returns_200(self):
        response = self.client.get(self._client_detail_url(self.primary_client))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.context["client_breadcrumbs"],
            [
                {"label": "Clientes", "url": reverse("core:client_list")},
                {"label": self.primary_client.name, "url": ""},
            ],
        )
        self.assertContains(response, 'aria-label="Ruta de clientes"')
        self.assertContains(response, f'href="{reverse("core:client_list")}"')

    def test_client_detail_view_renders_client_base_data(self):
        self.primary_client.phone = "+34 600 111 111"
        self.primary_client.email = "claudia@example.com"
        self.primary_client.notes = "Cliente habitual"
        self.primary_client.save(update_fields=["phone", "email", "notes"])

        response = self.client.get(self._client_detail_url(self.primary_client))

        self.assertContains(response, self.primary_client.name)
        self.assertContains(response, self.primary_client.phone)
        self.assertContains(response, self.primary_client.email)
        self.assertContains(response, self.primary_client.notes)

    def test_client_detail_view_exposes_new_appointment_and_edit_actions(self):
        next_url = reverse("core:app_entrypoint")
        detail_url = self._client_detail_url(self.primary_client, next_url=next_url)
        expected_appointment_create_url = (
            f"{reverse('core:appointment_create')}?"
            f"{urlencode({'next': detail_url, 'client': self.primary_client.pk})}"
        )
        expected_client_update_url = (
            f"{reverse('core:client_update', args=[self.primary_client.pk])}"
            f"?{urlencode({'next': detail_url})}"
        )
        expected_client_archive_url = (
            f"{reverse('core:client_archive', args=[self.primary_client.pk])}"
            f"?{urlencode({'next': detail_url})}"
        )

        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["appointment_create_url"], expected_appointment_create_url)
        self.assertEqual(response.context["client_update_url"], expected_client_update_url)
        self.assertEqual(response.context["client_archive_url"], expected_client_archive_url)
        self.assertContains(response, "Nueva cita")
        self.assertContains(response, "Editar cliente")
        self.assertContains(response, "Archivar cliente")
        self.assertContains(response, expected_appointment_create_url.replace("&", "&amp;"))
        self.assertContains(response, expected_client_update_url.replace("&", "&amp;"))
        self.assertContains(response, expected_client_archive_url.replace("&", "&amp;"))

    def test_client_detail_view_preserves_client_list_as_natural_back_context(self):
        list_url = reverse("core:client_list")
        detail_url = self._client_detail_url(self.primary_client, next_url=list_url)
        expected_appointment_create_url = (
            f"{reverse('core:appointment_create')}?"
            f"{urlencode({'next': detail_url, 'client': self.primary_client.pk})}"
        )
        expected_client_update_url = (
            f"{reverse('core:client_update', args=[self.primary_client.pk])}"
            f"?{urlencode({'next': detail_url})}"
        )
        expected_client_archive_url = (
            f"{reverse('core:client_archive', args=[self.primary_client.pk])}"
            f"?{urlencode({'next': detail_url})}"
        )

        response = self.client.get(detail_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["back_url"], list_url)
        self.assertEqual(response.context["appointment_create_url"], expected_appointment_create_url)
        self.assertEqual(response.context["client_update_url"], expected_client_update_url)
        self.assertEqual(response.context["client_archive_url"], expected_client_archive_url)
        self.assertContains(response, f'href="{list_url}"')
        self.assertContains(response, expected_appointment_create_url.replace("&", "&amp;"))
        self.assertContains(response, expected_client_update_url.replace("&", "&amp;"))
        self.assertContains(response, expected_client_archive_url.replace("&", "&amp;"))

    def test_client_archive_view_confirms_and_archives_client_and_cancels_future_active_appointments(self):
        list_url = reverse("core:client_list")
        archived_list_url = reverse("core:archived_client_list")
        past_day = self._previous_working_day(start_day=self.operational_day)
        future_day = self._next_working_day(start_day=self.operational_day)
        later_future_day = self._next_working_day(start_day=future_day)

        past_appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            past_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        future_confirmed = self._create_appointment(
            self.primary_client,
            self.review_service,
            future_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        future_pending = self._create_appointment(
            self.primary_client,
            self.control_service,
            later_future_day,
            time(10, 0),
            Appointment.Status.PENDING,
        )
        future_cancelled = self._create_existing_cancelled_appointment(
            self.primary_client,
            self.control_service,
            future_day,
            time(11, 0),
        )
        detail_url = self._client_detail_url(self.primary_client, next_url=list_url)
        archive_url = (
            f"{reverse('core:client_archive', args=[self.primary_client.pk])}"
            f"?{urlencode({'next': detail_url})}"
        )

        get_response = self.client.get(archive_url)

        self.assertEqual(get_response.status_code, 200)
        self.assertTemplateUsed(get_response, "core/client_archive_confirm.html")
        self.assertContains(get_response, "Archivar cliente")
        self.assertContains(get_response, "listado activo")
        self.assertContains(get_response, "citas futuras activas se cancelaran automaticamente")
        self.assertContains(get_response, "historico seguira conservado")
        self.assertContains(get_response, "Clientes archivados")
        self.assertContains(get_response, "Confirmar archivado")

        post_response = self.client.post(archive_url)

        self.assertRedirects(post_response, archived_list_url, fetch_redirect_response=False)
        self.primary_client.refresh_from_db()
        past_appointment.refresh_from_db()
        future_confirmed.refresh_from_db()
        future_pending.refresh_from_db()
        future_cancelled.refresh_from_db()

        self.assertTrue(self.primary_client.is_archived)
        self.assertEqual(past_appointment.status, Appointment.Status.CONFIRMED)
        self.assertEqual(future_confirmed.status, Appointment.Status.CANCELLED)
        self.assertEqual(future_pending.status, Appointment.Status.CANCELLED)
        self.assertEqual(future_cancelled.status, Appointment.Status.CANCELLED)

        active_list_response = self.client.get(list_url)
        archived_list_response = self.client.get(archived_list_url)
        archived_detail_response = self.client.get(reverse("core:client_detail", args=[self.primary_client.pk]))

        self.assertNotContains(active_list_response, self.primary_client.name)
        self.assertContains(archived_list_response, self.primary_client.name)
        self.assertEqual(archived_detail_response.context["back_url"], archived_list_url)
        self.assertNotContains(archived_detail_response, "Nueva cita")
        self.assertNotContains(archived_detail_response, "Editar cliente")
        self.assertNotContains(archived_detail_response, "Archivar cliente")
        self.assertNotContains(archived_detail_response, "Editar cita")
        self.assertNotContains(archived_detail_response, "Sin accion operativa")
        self.assertNotContains(archived_detail_response, "Accion")
        self.assertContains(archived_detail_response, "Archivado")
        self.assertEqual(
            archived_detail_response.context["client_breadcrumbs"],
            [
                {"label": "Clientes", "url": reverse("core:client_list")},
                {"label": "Clientes archivados", "url": reverse("core:archived_client_list")},
                {"label": self.primary_client.name, "url": ""},
            ],
        )

    def test_client_reactivate_view_confirms_and_restores_archived_client_to_active_list(self):
        list_url = reverse("core:client_list")
        archived_list_url = reverse("core:archived_client_list")
        self.primary_client.is_archived = True
        self.primary_client.save(update_fields=["is_archived"])
        reactivate_url = (
            f"{reverse('core:client_reactivate', args=[self.primary_client.pk])}"
            f"?{urlencode({'next': archived_list_url})}"
        )

        get_response = self.client.get(reactivate_url)

        self.assertEqual(get_response.status_code, 200)
        self.assertTemplateUsed(get_response, "core/client_reactivate_confirm.html")
        self.assertContains(get_response, "Reactivar cliente")
        self.assertContains(get_response, "Confirmar reactivacion")
        self.assertContains(get_response, self.primary_client.name)

        post_response = self.client.post(reactivate_url)

        self.assertRedirects(post_response, list_url, fetch_redirect_response=False)
        self.primary_client.refresh_from_db()
        self.assertFalse(self.primary_client.is_archived)
        self.assertContains(self.client.get(list_url), self.primary_client.name)
        self.assertNotContains(self.client.get(archived_list_url), self.primary_client.name)

    def test_client_delete_view_deletes_archived_client_without_history(self):
        archived_list_url = reverse("core:archived_client_list")
        self.secondary_client.is_archived = True
        self.secondary_client.save(update_fields=["is_archived"])
        delete_url = (
            f"{reverse('core:client_delete', args=[self.secondary_client.pk])}"
            f"?{urlencode({'next': archived_list_url})}"
        )

        get_response = self.client.get(delete_url)

        self.assertEqual(get_response.status_code, 200)
        self.assertTemplateUsed(get_response, "core/client_delete_confirm.html")
        self.assertContains(get_response, "Eliminar definitivamente")
        self.assertContains(get_response, "Confirmar eliminacion definitiva")
        self.assertContains(get_response, "No tiene citas vinculadas")
        self.assertContains(get_response, "borrado en cascada")

        post_response = self.client.post(delete_url)

        self.assertRedirects(post_response, archived_list_url, fetch_redirect_response=False)
        self.assertFalse(Client.objects.filter(pk=self.secondary_client.pk).exists())

    def test_client_delete_view_deletes_archived_client_with_history_in_cascade(self):
        archived_list_url = reverse("core:archived_client_list")
        appointment = self._create_existing_cancelled_appointment(
            self.primary_client,
            self.review_service,
            self.operational_day,
            time(9, 0),
        )
        self.primary_client.is_archived = True
        self.primary_client.save(update_fields=["is_archived"])
        delete_url = (
            f"{reverse('core:client_delete', args=[self.primary_client.pk])}"
            f"?{urlencode({'next': archived_list_url})}"
        )

        get_response = self.client.get(delete_url)

        self.assertEqual(get_response.status_code, 200)
        self.assertTemplateUsed(get_response, "core/client_delete_confirm.html")
        self.assertContains(get_response, "Tiene")
        self.assertContains(get_response, "citas vinculadas")
        self.assertContains(get_response, "Confirmar eliminacion definitiva")

        post_response = self.client.post(delete_url)

        self.assertRedirects(post_response, archived_list_url, fetch_redirect_response=False)
        self.assertFalse(Client.objects.filter(pk=self.primary_client.pk).exists())
        self.assertFalse(Appointment.objects.filter(pk=appointment.pk).exists())

    def test_client_detail_view_renders_history_in_descending_order_with_active_and_cancelled_appointments(self):
        today = self.operational_day
        previous_day = self._previous_working_day(start_day=today)
        evaluation_service = Service.objects.create(name="Evaluacion", color="#AE4C42")

        newest_appointment = self._create_appointment(
            self.primary_client,
            evaluation_service,
            today,
            time(12, 0),
            Appointment.Status.CONFIRMED,
        )
        middle_appointment = self._create_appointment(
            self.primary_client,
            self.control_service,
            today,
            time(9, 0),
            Appointment.Status.PENDING,
        )
        oldest_appointment = self._create_existing_cancelled_appointment(
            self.primary_client,
            self.review_service,
            previous_day,
            time(10, 0),
            initial_status=Appointment.Status.PENDING,
        )

        response = self.client.get(self._client_detail_url(self.primary_client))
        content = response.content.decode()

        self.assertContains(response, "Evaluacion")
        self.assertContains(response, "Control")
        self.assertContains(response, "Revision")
        self.assertContains(response, "Confirmada")
        self.assertContains(response, "Pendiente")
        self.assertContains(response, "Cancelada")
        history_urls = [item["edit_url"] for item in response.context["client_history"]]
        repeat_urls = [item["repeat_url"] for item in response.context["client_history"]]
        self.assertEqual(
            history_urls,
            [
                f"{reverse('core:appointment_update', args=[newest_appointment.pk])}"
                f"?{urlencode({'year': today.year, 'month': today.month, 'day': today.day})}",
                f"{reverse('core:appointment_update', args=[middle_appointment.pk])}"
                f"?{urlencode({'year': today.year, 'month': today.month, 'day': today.day})}",
                "",
            ],
        )
        self.assertEqual(
            repeat_urls,
            [
                (
                    f"{reverse('core:appointment_create')}?"
                    f"{urlencode({'next': reverse('core:client_detail', args=[self.primary_client.pk]), 'source_appointment': newest_appointment.pk})}"
                ),
                (
                    f"{reverse('core:appointment_create')}?"
                    f"{urlencode({'next': reverse('core:client_detail', args=[self.primary_client.pk]), 'source_appointment': middle_appointment.pk})}"
                ),
                (
                    f"{reverse('core:appointment_create')}?"
                    f"{urlencode({'next': reverse('core:client_detail', args=[self.primary_client.pk]), 'source_appointment': oldest_appointment.pk})}"
                ),
            ],
        )
        self.assertContains(response, "Repetir cita", count=3)
        self.assertContains(response, "Sin accion operativa")
        self.assertLess(content.index("Evaluacion"), content.index("Control"))
        self.assertLess(content.index("Control"), content.index("Revision"))

    def test_archived_client_detail_does_not_expose_repeat_action(self):
        self.primary_client.is_archived = True
        self.primary_client.save(update_fields=["is_archived"])
        self._create_existing_cancelled_appointment(
            self.primary_client,
            self.review_service,
            self.operational_day,
            time(10, 0),
        )

        response = self.client.get(self._client_detail_url(self.primary_client))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Repetir cita")
        self.assertTrue(all(item["repeat_url"] == "" for item in response.context["client_history"]))

    def test_client_detail_view_uses_next_for_back_link(self):
        today = self.operational_day
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        next_url = (
            f"{reverse('core:appointment_update', args=[appointment.pk])}"
            f"?{urlencode({'year': today.year, 'month': today.month, 'day': today.day})}"
        )

        response = self.client.get(self._client_detail_url(self.primary_client, next_url=next_url))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["back_url"], next_url)
        self.assertContains(response, reverse("core:appointment_update", args=[appointment.pk]))

    def test_client_detail_view_falls_back_to_app_when_next_is_missing(self):
        response = self.client.get(self._client_detail_url(self.primary_client))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["back_url"], reverse("core:app_entrypoint"))
        self.assertContains(response, f'href="{reverse("core:app_entrypoint")}"')

    def test_client_update_view_prefills_form_and_uses_next_for_back_url(self):
        self.primary_client.phone = "+34 600 111 111"
        self.primary_client.email = "claudia@example.com"
        self.primary_client.notes = "Cliente habitual"
        self.primary_client.save(update_fields=["phone", "email", "notes"])
        next_url = self._client_detail_url(self.primary_client, next_url=reverse("core:app_entrypoint"))

        response = self.client.get(
            reverse("core:client_update", args=[self.primary_client.pk]),
            {"next": next_url},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/client_form.html")
        self.assertEqual(response.context["back_url"], next_url)
        self.assertEqual(
            response.context["client_breadcrumbs"],
            [
                {"label": "Clientes", "url": reverse("core:client_list")},
                {"label": self.primary_client.name, "url": next_url},
                {"label": "Editar", "url": ""},
            ],
        )
        self.assertContains(response, "Editar cliente")
        self.assertContains(response, "Volver a ficha")
        self.assertContains(response, 'name="next"')
        self.assertContains(response, 'aria-label="Ruta de clientes"')
        self.assertContains(response, f'href="{next_url}"')
        self.assertEqual(response.context["form"]["name"].value(), self.primary_client.name)
        self.assertEqual(response.context["form"]["phone"].value(), self.primary_client.phone)
        self.assertEqual(response.context["form"]["email"].value(), self.primary_client.email)
        self.assertEqual(response.context["form"]["notes"].value(), self.primary_client.notes)

    def test_client_update_view_saves_changes_and_returns_to_client_detail_context(self):
        next_url = self._client_detail_url(self.primary_client, next_url=reverse("core:app_entrypoint"))

        response = self.client.post(
            reverse("core:client_update", args=[self.primary_client.pk]),
            {
                "name": "Claudia Ajustada",
                "phone": "+34 600 222 222",
                "email": "claudia-ajustada@example.com",
                "notes": "Seguimiento prioritario",
                "next": next_url,
            },
        )

        self.assertRedirects(response, next_url, fetch_redirect_response=False)
        self.primary_client.refresh_from_db()
        self.assertEqual(self.primary_client.name, "Claudia Ajustada")
        self.assertEqual(self.primary_client.phone, "+34 600 222 222")
        self.assertEqual(self.primary_client.email, "claudia-ajustada@example.com")
        self.assertEqual(self.primary_client.notes, "Seguimiento prioritario")


class ClientListViewTests(AuthenticatedAgendaBaseTestCase):
    def test_client_list_view_returns_200_and_renders_clients(self):
        expected_client_create_url = (
            f"{reverse('core:client_create')}?{urlencode({'next': reverse('core:client_list')})}"
        )
        archived_client_list_url = reverse("core:archived_client_list")
        expected_primary_detail_url = (
            f"{reverse('core:client_detail', args=[self.primary_client.pk])}"
            f"?{urlencode({'next': reverse('core:client_list')})}"
        )
        response = self.client.get(reverse("core:client_list"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/client_list.html")
        self.assertEqual(
            response.context["client_breadcrumbs"],
            [
                {"label": "Clientes", "url": reverse("core:client_list")},
            ],
        )
        self.assertContains(response, "Clientes")
        self.assertContains(response, "Consulta clientes, accede a su historial y da de alta nuevos clientes.")
        self.assertContains(response, "Proxima cita")
        self.assertContains(response, 'aria-label="Ruta de clientes"')
        self.assertContains(response, self.primary_client.name)
        self.assertContains(response, self.secondary_client.name)
        self.assertContains(response, expected_primary_detail_url.replace("&", "&amp;"))
        self.assertContains(response, "Sin proxima cita", count=Client.objects.count())
        self.assertContains(response, "Nuevo cliente")
        self.assertContains(response, "Clientes archivados")
        self.assertContains(response, archived_client_list_url)
        self.assertContains(response, expected_client_create_url.replace("&", "&amp;"))

    def test_client_list_view_exposes_direct_new_appointment_action_per_client(self):
        list_url = reverse("core:client_list")
        expected_primary_appointment_url = (
            f"{reverse('core:appointment_create')}?"
            f"{urlencode({'next': list_url, 'client': self.primary_client.pk})}"
        )
        expected_secondary_appointment_url = (
            f"{reverse('core:appointment_create')}?"
            f"{urlencode({'next': list_url, 'client': self.secondary_client.pk})}"
        )

        response = self.client.get(list_url)
        clients = {client.pk: client for client in response.context["clients"]}

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            clients[self.primary_client.pk].appointment_create_url,
            expected_primary_appointment_url,
        )
        self.assertEqual(
            clients[self.secondary_client.pk].appointment_create_url,
            expected_secondary_appointment_url,
        )
        self.assertContains(response, "Nueva cita", count=Client.objects.count())
        self.assertContains(response, expected_primary_appointment_url.replace("&", "&amp;"))
        self.assertContains(response, expected_secondary_appointment_url.replace("&", "&amp;"))

    def test_client_list_excludes_archived_clients_and_archived_list_shows_them(self):
        archived_client_list_url = reverse("core:archived_client_list")
        self.secondary_client.is_archived = True
        self.secondary_client.save(update_fields=["is_archived"])
        expected_detail_url = (
            f"{reverse('core:client_detail', args=[self.secondary_client.pk])}"
            f"?{urlencode({'next': archived_client_list_url})}"
        )
        expected_reactivate_url = (
            f"{reverse('core:client_reactivate', args=[self.secondary_client.pk])}"
            f"?{urlencode({'next': archived_client_list_url})}"
        )
        expected_delete_url = (
            f"{reverse('core:client_delete', args=[self.secondary_client.pk])}"
            f"?{urlencode({'next': archived_client_list_url})}"
        )

        active_response = self.client.get(reverse("core:client_list"))
        archived_response = self.client.get(archived_client_list_url)

        self.assertEqual(active_response.status_code, 200)
        self.assertNotContains(active_response, self.secondary_client.name)
        self.assertContains(active_response, self.primary_client.name)
        self.assertEqual(archived_response.status_code, 200)
        self.assertTemplateUsed(archived_response, "core/archived_client_list.html")
        self.assertContains(archived_response, "Clientes archivados")
        self.assertContains(archived_response, self.secondary_client.name)
        self.assertNotContains(archived_response, self.primary_client.name)
        self.assertContains(archived_response, reverse("core:client_list"))
        self.assertContains(archived_response, "Ver ficha")
        self.assertContains(archived_response, "Reactivar")
        self.assertContains(archived_response, "Eliminar definitivamente")
        self.assertContains(archived_response, "Sin citas vinculadas")
        self.assertContains(archived_response, expected_detail_url.replace("&", "&amp;"))
        self.assertContains(archived_response, expected_reactivate_url.replace("&", "&amp;"))
        self.assertContains(archived_response, expected_delete_url.replace("&", "&amp;"))

    def test_client_list_view_renders_empty_state_with_create_action(self):
        expected_client_create_url = (
            f"{reverse('core:client_create')}?{urlencode({'next': reverse('core:client_list')})}"
        )
        Client.objects.all().delete()

        response = self.client.get(reverse("core:client_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sin clientes")
        self.assertContains(response, "Puedes crear el primero desde esta pantalla.")
        self.assertContains(response, "Nuevo cliente")
        self.assertContains(response, expected_client_create_url.replace("&", "&amp;"))

    @patch("core.views.timezone.now")
    def test_client_list_view_shows_nearest_valid_future_appointment(self, mocked_now):
        today = self.operational_day
        mocked_now.return_value = timezone.make_aware(
            datetime.combine(today, time(8, 30)),
            timezone.get_current_timezone(),
        )
        previous_day = self._previous_working_day(start_day=today)
        later_day = self._next_working_day(start_day=today)
        self._create_appointment(
            self.primary_client,
            self.review_service,
            previous_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_existing_cancelled_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
        )
        self._create_appointment(
            self.primary_client,
            self.control_service,
            today,
            time(10, 0),
            Appointment.Status.PENDING,
        )
        self._create_appointment(
            self.primary_client,
            self.review_service,
            later_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.get(reverse("core:client_list"))
        clients = {client.pk: client for client in response.context["clients"]}
        expected_label = f"{_format_compact_day(today)} · 10:00"

        self.assertEqual(response.status_code, 200)
        self.assertEqual(clients[self.primary_client.pk].next_appointment_label, expected_label)
        self.assertEqual(clients[self.secondary_client.pk].next_appointment_label, "Sin proxima cita")
        self.assertContains(response, expected_label)
        self.assertNotContains(response, f"{_format_compact_day(today)} · 09:00")

    @patch("core.views.timezone.now")
    def test_client_list_view_shows_simple_fallback_when_only_past_or_cancelled_appointments_exist(
        self,
        mocked_now,
    ):
        today = self.operational_day
        mocked_now.return_value = timezone.make_aware(
            datetime.combine(today, time(8, 30)),
            timezone.get_current_timezone(),
        )
        previous_day = self._previous_working_day(start_day=today)
        self._create_appointment(
            self.primary_client,
            self.review_service,
            previous_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_existing_cancelled_appointment(
            self.primary_client,
            self.control_service,
            today,
            time(10, 0),
        )

        response = self.client.get(reverse("core:client_list"))
        clients = {client.pk: client for client in response.context["clients"]}

        self.assertEqual(response.status_code, 200)
        self.assertEqual(clients[self.primary_client.pk].next_appointment_label, "Sin proxima cita")
        self.assertContains(response, "Sin proxima cita")


class AppEntryPointViewTests(AuthenticatedAgendaBaseTestCase):
    def _day_context(self, response, target_day):
        for week in response.context["agenda_weeks"]:
            for week_day in week:
                if week_day["iso_date"] == target_day.isoformat():
                    return week_day
        self.fail(f"Day {target_day.isoformat()} not found in agenda_weeks context.")

    def _slot_context(self, response, slot_time):
        for slot in response.context["agenda_timeline_slots"]:
            if slot["time"] == slot_time:
                return slot
        self.fail(f"Slot {slot_time} not found in agenda_timeline_slots.")

    def test_agenda_metrics_and_daily_states_use_real_data(self):
        selected_day = self._next_working_day(start_day=self.real_today, include_today=False)
        self._create_block(selected_day, "16:00", label="Bloqueo interno")
        first_appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            selected_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            selected_day,
            time(10, 0),
            Appointment.Status.PENDING,
        )
        self._create_appointment(
            self.primary_client,
            self.review_service,
            selected_day,
            time(12, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_existing_cancelled_appointment(
            self.tertiary_client,
            self.review_service,
            selected_day,
            time(17, 0),
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )
        slot_11 = self._slot_context(response, "11:00")
        slot_13 = self._slot_context(response, "13:00")

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(selected_day, self.real_today)
        self.assertContains(response, "Claudia Real")
        self.assertContains(response, "10:00")
        self.assertContains(response, "Bloqueo interno")
        self.assertEqual(slot_11["available_label"], "Disponible")
        self.assertEqual(slot_13["available_label"], "Disponible")
        self.assertEqual(slot_13["unavailable_label"], "")
        self.assertNotContains(response, "Nora Real")
        self.assertContains(response, reverse("core:appointment_create"))
        self.assertContains(response, reverse("core:appointment_update", args=[first_appointment.pk]))
        self.assertContains(response, "ocupan agenda ese dia")
        self.assertContains(response, "citas por confirmar")
        self.assertContains(response, "citas ya confirmadas")
        self.assertContains(response, "tramos donde aun puedes citar")
        self.assertNotContains(response, response.context["selected_day_summary"])
        self.assertNotContains(response, "pending + confirmed del dia")
        self.assertNotContains(response, "Tramos con citas")
        self.assertNotContains(response, "Canceladas")

        metrics = {metric["label"]: metric["value"] for metric in response.context["agenda_metrics"]}
        self.assertEqual(list(metrics.keys()), ["Citas activas", "Pendientes", "Confirmadas", "Huecos libres"])
        self.assertEqual(metrics["Citas activas"], "03")
        self.assertEqual(metrics["Pendientes"], "01")
        self.assertEqual(metrics["Confirmadas"], "02")
        self.assertEqual(metrics["Huecos libres"], "07")

    def test_operational_day_without_weekly_availability_shows_all_base_slots_as_available(self):
        today = self.operational_day

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["selected_day_is_working_day"])
        self.assertEqual(len(response.context["agenda_timeline_slots"]), 8)
        self.assertEqual(
            [slot["time"] for slot in response.context["agenda_timeline_slots"]],
            ["09:00", "10:00", "11:00", "12:00", "13:00", "16:00", "17:00", "18:00"],
        )
        self.assertTrue(all(slot["capacity"] == 3 for slot in response.context["agenda_timeline_slots"]))
        self.assertTrue(all(slot["available_label"] == "Disponible" for slot in response.context["agenda_timeline_slots"]))
        self.assertTrue(all(slot["unavailable_label"] == "" for slot in response.context["agenda_timeline_slots"]))

    def test_cancelled_appointments_do_not_reappear_in_metrics_or_day_panel(self):
        today = self.operational_day
        selected_day = self._next_weekday(3, start_day=today)
        self._create_existing_cancelled_appointment(
            self.tertiary_client,
            self.review_service,
            selected_day,
            time(9, 0),
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        slot_09 = self._slot_context(response, "09:00")
        metrics = {metric["label"]: metric["value"] for metric in response.context["agenda_metrics"]}

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(metrics.keys()), ["Citas activas", "Pendientes", "Confirmadas", "Huecos libres"])
        self.assertEqual(metrics["Citas activas"], "00")
        self.assertEqual(metrics["Pendientes"], "00")
        self.assertEqual(metrics["Confirmadas"], "00")
        self.assertEqual(metrics["Huecos libres"], "08")
        self.assertEqual(slot_09["entries"], [])
        self.assertEqual(slot_09["available_label"], "Disponible")
        self.assertNotContains(response, self.tertiary_client.name)
        self.assertNotContains(response, "Canceladas")

    def test_free_slots_metric_counts_only_slots_that_can_still_accept_new_appointments(self):
        today = self.operational_day
        self._create_block(today, "11:00", label="Bloqueo interno")
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(9, 0),
            Appointment.Status.PENDING,
        )
        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.fourth_client,
            self.control_service,
            today,
            time(10, 0),
            Appointment.Status.PENDING,
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        slot_09 = self._slot_context(response, "09:00")
        slot_10 = self._slot_context(response, "10:00")
        slot_11 = self._slot_context(response, "11:00")
        metrics = {metric["label"]: metric["value"] for metric in response.context["agenda_metrics"]}

        self.assertEqual(metrics["Huecos libres"], "06")
        self.assertFalse(slot_09["can_book"])
        self.assertEqual(slot_09["create_url"], "")
        self.assertTrue(slot_10["can_book"])
        self.assertNotEqual(slot_10["create_url"], "")
        self.assertFalse(slot_11["can_book"])
        self.assertEqual(slot_11["create_url"], "")

    def test_daily_panel_complete_slot_keeps_edit_links_and_hides_create_action(self):
        today = self.operational_day
        agenda_query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        expected_next = f"{reverse('core:app_entrypoint')}?{agenda_query}"
        expected_create_url = (
            f"{reverse('core:appointment_create')}"
            f"?{urlencode({'year': today.year, 'month': today.month, 'day': today.day, 'slot_time': '09:00', 'next': expected_next})}"
        )
        expected_create_href = expected_create_url.replace("&", "&amp;")
        first_appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        second_appointment = self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(9, 0),
            Appointment.Status.PENDING,
        )
        third_appointment = self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        slot_09 = self._slot_context(response, "09:00")
        self.assertEqual(len(slot_09["entries"]), 3)
        self.assertEqual(slot_09["complete_label"], "Completo")
        self.assertContains(response, 'class="agenda-slot__entry-card"', count=3)
        self.assertContains(response, 'class="agenda-slot__time-meta agenda-slot__time-meta--complete"')
        self.assertContains(response, reverse("core:appointment_update", args=[first_appointment.pk]))
        self.assertContains(response, reverse("core:appointment_update", args=[second_appointment.pk]))
        self.assertContains(response, reverse("core:appointment_update", args=[third_appointment.pk]))
        self.assertNotContains(response, 'class="agenda-slot__entry-link"')
        self.assertNotContains(response, 'class="agenda-slot__state agenda-slot__state--complete"')
        self.assertNotContains(response, expected_create_href)
        self.assertNotIn(">Editar<", response.content.decode())
        self.assertEqual(slot_09["create_url"], "")

    def test_month_markers_keep_active_count_and_add_block_signal(self):
        today = self.operational_day
        selected_day = self._next_weekday(4, start_day=today)
        self._create_block(selected_day, "11:00")
        self._create_appointment(
            self.primary_client,
            self.review_service,
            selected_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            selected_day,
            time(10, 0),
            Appointment.Status.PENDING,
        )
        self._create_existing_cancelled_appointment(
            self.tertiary_client,
            self.review_service,
            selected_day,
            time(10, 0),
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        day_context = self._day_context(response, selected_day)
        self.assertEqual(
            [marker["label"] for marker in day_context["markers"]],
            ["2 citas", "1 bloqueo"],
        )
        self.assertIsNone(day_context["primary_state"])
        self.assertEqual(day_context["secondary_holiday_marker"], "")

    def test_month_and_panel_mark_saturday_as_non_working(self):
        selected_day = self._next_weekday(5)

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        day_context = self._day_context(response, selected_day)
        slot_09 = self._slot_context(response, "09:00")

        self.assertEqual(day_context["status_kind"], "non-working")
        self.assertEqual(day_context["primary_state"]["kind"], "non-working")
        self.assertEqual(day_context["primary_state"]["lines"], ["Sábado", "no laborable"])
        self.assertEqual(day_context["markers"], [])
        self.assertEqual(day_context["secondary_holiday_marker"], "")
        self.assertFalse(response.context["selected_day_is_working_day"])
        self.assertEqual(response.context["selected_day_status_label"], "Sabado no laborable")
        self.assertEqual(slot_09["create_url"], "")
        self.assertEqual(slot_09["block_action_label"], "")
        self.assertEqual(slot_09["available_label"], "")
        self.assertEqual(slot_09["unavailable_label"], "Sabado no laborable")
        self.assertContains(response, 'class="agenda-day agenda-day--selected agenda-day--non-working"')
        self.assertContains(response, 'class="agenda-day__state agenda-day__state--non-working"')
        self.assertContains(response, ">Sábado<")
        self.assertContains(response, ">no laborable<")
        self.assertContains(response, "Sabado no laborable")

    def test_month_and_panel_mark_sunday_as_non_working(self):
        selected_day = self._next_weekday(6)

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        day_context = self._day_context(response, selected_day)
        slot_09 = self._slot_context(response, "09:00")

        self.assertEqual(day_context["status_kind"], "non-working")
        self.assertEqual(day_context["primary_state"]["kind"], "non-working")
        self.assertEqual(day_context["primary_state"]["lines"], ["Domingo", "no laborable"])
        self.assertEqual(day_context["markers"], [])
        self.assertEqual(day_context["secondary_holiday_marker"], "")
        self.assertFalse(response.context["selected_day_is_working_day"])
        self.assertEqual(response.context["selected_day_status_label"], "Domingo no laborable")
        self.assertEqual(slot_09["create_url"], "")
        self.assertEqual(slot_09["block_action_label"], "")
        self.assertEqual(slot_09["unavailable_label"], "Domingo no laborable")
        self.assertContains(response, 'class="agenda-day agenda-day--selected agenda-day--non-working"')
        self.assertContains(response, 'class="agenda-day__state agenda-day__state--non-working"')
        self.assertContains(response, ">Domingo<")
        self.assertContains(response, ">no laborable<")
        self.assertContains(response, "Domingo no laborable")

    def test_month_grid_shows_secondary_official_holiday_marker_when_toggle_is_disabled(self):
        selected_day = date(2026, 4, 23)
        settings = AgendaSettings.get_solo()
        settings.official_holidays_non_working = False
        settings.save(update_fields=["official_holidays_non_working"])
        OfficialHoliday.objects.create(day=selected_day, name="San Jorge")

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        day_context = self._day_context(response, selected_day)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["selected_day_is_working_day"])
        self.assertEqual(day_context["status_kind"], "")
        self.assertIsNone(day_context["primary_state"])
        self.assertEqual(day_context["secondary_holiday_marker"], "San Jorge")
        self.assertEqual(
            day_context["secondary_holiday_marker_title"],
            "San Jorge. La agenda esta abierta.",
        )
        self.assertContains(response, ">San Jorge<")
        self.assertContains(
            response,
            'title="San Jorge. La agenda esta abierta."',
        )
        self.assertNotContains(response, ">F. oficial<")

    def test_month_grid_hides_secondary_official_holiday_marker_when_day_has_no_holiday(self):
        selected_day = date(2026, 4, 22)

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        day_context = self._day_context(response, selected_day)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(day_context["primary_state"])
        self.assertEqual(day_context["secondary_holiday_marker"], "")
        self.assertEqual(day_context["secondary_holiday_marker_title"], "")
        self.assertNotContains(response, ">F. oficial<")
        self.assertNotContains(response, ">San Jorge<")

    def test_month_grid_hides_secondary_marker_when_official_holiday_blocks_operatively(self):
        selected_day = date(2026, 4, 23)
        OfficialHoliday.objects.create(day=selected_day, name="San Jorge")

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        day_context = self._day_context(response, selected_day)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["selected_day_is_working_day"])
        self.assertEqual(day_context["status_kind"], "official-holiday")
        self.assertEqual(day_context["primary_state"]["kind"], "official-holiday")
        self.assertEqual(day_context["primary_state"]["lines"], ["San Jorge"])
        self.assertEqual(day_context["secondary_holiday_marker"], "")
        self.assertEqual(day_context["secondary_holiday_marker_title"], "")
        self.assertEqual(day_context["markers"], [])
        self.assertContains(response, 'class="agenda-day agenda-day--selected agenda-day--official-holiday"')
        self.assertContains(response, 'class="agenda-day__state agenda-day__state--official-holiday"')
        self.assertContains(response, ">San Jorge<")
        self.assertNotContains(response, ">F. oficial<")

    def test_month_grid_keeps_non_blocking_official_holiday_out_of_blocking_holiday_treatment(self):
        selected_day = date(2026, 4, 23)
        settings = AgendaSettings.get_solo()
        settings.official_holidays_non_working = False
        settings.save(update_fields=["official_holidays_non_working"])
        OfficialHoliday.objects.create(day=selected_day, name="San Jorge")

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        day_context = self._day_context(response, selected_day)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["selected_day_is_working_day"])
        self.assertIsNone(day_context["primary_state"])
        self.assertEqual(day_context["secondary_holiday_marker"], "San Jorge")
        self.assertEqual(day_context["markers"], [])
        self.assertNotContains(response, 'agenda-day--official-holiday')
        self.assertNotContains(response, 'agenda-day__marker--official-holiday')
        self.assertNotContains(response, ">F. oficial<")

    def test_daily_panel_shows_secondary_official_holiday_notice_when_toggle_is_disabled(self):
        selected_day = date(2026, 4, 23)
        settings = AgendaSettings.get_solo()
        settings.official_holidays_non_working = False
        settings.save(update_fields=["official_holidays_non_working"])
        OfficialHoliday.objects.create(day=selected_day, name="San Jorge")

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["selected_day_is_working_day"])
        self.assertEqual(
            response.context["selected_day_secondary_notice"],
            "Es San Jorge, pero la agenda esta abierta.",
        )
        self.assertContains(response, "Es San Jorge, pero la agenda esta abierta.")
        self.assertNotContains(response, "Festivo oficial registrado:")
        self.assertNotContains(response, 'class="agenda-panel__status agenda-panel__status--working"')

    def test_daily_panel_hides_secondary_official_holiday_notice_when_day_has_no_holiday(self):
        selected_day = date(2026, 4, 22)

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_day_secondary_notice"], "")
        self.assertNotContains(response, "Festivo oficial registrado:")

    def test_daily_panel_hides_secondary_notice_when_official_holiday_blocks_operatively(self):
        selected_day = date(2026, 4, 23)
        OfficialHoliday.objects.create(day=selected_day, name="San Jorge")

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["selected_day_is_working_day"])
        self.assertEqual(response.context["selected_day_status_label"], "San Jorge")
        self.assertEqual(response.context["selected_day_secondary_notice"], "")
        self.assertNotContains(response, "Festivo oficial registrado:")

    def test_daily_panel_hides_secondary_notice_when_weekend_remains_non_working(self):
        selected_day = self._next_weekday(5)
        settings = AgendaSettings.get_solo()
        settings.official_holidays_non_working = False
        settings.save(update_fields=["official_holidays_non_working"])
        OfficialHoliday.objects.create(day=selected_day, name="Festivo autonomico")

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["selected_day_is_working_day"])
        self.assertEqual(response.context["selected_day_status_label"], "Sabado no laborable")
        self.assertEqual(response.context["selected_day_secondary_notice"], "")
        self.assertNotContains(response, "Festivo oficial registrado:")

    def test_manual_closure_has_priority_over_weekend_in_month_and_panel(self):
        selected_day = self._next_weekday(5)
        ManualClosure.objects.create(
            start_date=selected_day,
            end_date=selected_day,
            reason_type=ManualClosure.ReasonType.VACATION,
            label="Vacaciones de abril",
            notes="Cierre completo del sabado",
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        day_context = self._day_context(response, selected_day)
        slot_09 = self._slot_context(response, "09:00")

        self.assertEqual(day_context["status_kind"], "manual-closure")
        self.assertEqual(day_context["primary_state"]["kind"], "manual-closure")
        self.assertEqual(day_context["primary_state"]["lines"], ["Vacaciones de abril"])
        self.assertEqual(day_context["markers"], [])
        self.assertFalse(response.context["selected_day_is_working_day"])
        self.assertEqual(response.context["selected_day_status_kind"], "manual-closure")
        self.assertEqual(response.context["selected_day_status_label"], "Vacaciones de abril")
        self.assertEqual(response.context["selected_day_status_notes"], "Cierre completo del sabado")
        self.assertEqual(slot_09["create_url"], "")
        self.assertEqual(slot_09["block_action_label"], "")
        self.assertEqual(slot_09["unavailable_label"], "Vacaciones de abril")
        self.assertContains(response, 'class="agenda-day agenda-day--selected agenda-day--manual-closure"')
        self.assertContains(response, 'class="agenda-day__state agenda-day__state--manual-closure"')
        self.assertContains(response, "Vacaciones de abril")

    def test_daily_panel_empty_slot_exposes_create_action_with_contextual_url(self):
        today = self.operational_day
        agenda_query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        expected_next = f"{reverse('core:app_entrypoint')}?{agenda_query}"
        expected_create_url = (
            f"{reverse('core:appointment_create')}"
            f"?{urlencode({'year': today.year, 'month': today.month, 'day': today.day, 'slot_time': '10:00', 'next': expected_next})}"
        )
        expected_create_href = expected_create_url.replace("&", "&amp;")
        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        slot_10 = self._slot_context(response, "10:00")

        self.assertEqual(slot_10["create_url"], expected_create_url)
        self.assertEqual(slot_10["create_label"], "Nueva cita")
        self.assertContains(response, expected_create_href)

    def test_daily_panel_partially_occupied_slot_keeps_entries_and_create_action(self):
        today = self.operational_day
        agenda_query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        expected_next = f"{reverse('core:app_entrypoint')}?{agenda_query}"
        expected_create_url = (
            f"{reverse('core:appointment_create')}"
            f"?{urlencode({'year': today.year, 'month': today.month, 'day': today.day, 'slot_time': '09:00', 'next': expected_next})}"
        )
        expected_create_href = expected_create_url.replace("&", "&amp;")
        first_appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        second_appointment = self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(9, 0),
            Appointment.Status.PENDING,
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        slot_09 = self._slot_context(response, "09:00")

        self.assertEqual(len(slot_09["entries"]), 2)
        self.assertEqual(slot_09["complete_label"], "")
        self.assertEqual(slot_09["create_url"], expected_create_url)
        self.assertContains(response, reverse("core:appointment_update", args=[first_appointment.pk]))
        self.assertContains(response, reverse("core:appointment_update", args=[second_appointment.pk]))
        self.assertContains(response, expected_create_href)

    def test_daily_panel_keeps_appointment_block_available_and_unavailable_states(self):
        today = self.operational_day
        self._create_block(today, "10:00")
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        slot_09 = self._slot_context(response, "09:00")
        slot_10 = self._slot_context(response, "10:00")
        slot_11 = self._slot_context(response, "11:00")
        slot_12 = self._slot_context(response, "12:00")

        self.assertEqual(len(slot_09["entries"]), 1)
        self.assertEqual(slot_09["blocked_label"], "")
        self.assertEqual(slot_10["blocked_label"], "Bloqueo puntual")
        self.assertEqual(slot_11["available_label"], "Disponible")
        self.assertEqual(slot_12["available_label"], "Disponible")
        self.assertEqual(slot_12["unavailable_label"], "")

    def test_daily_panel_waits_for_third_active_entry_before_marking_capacity_three_slot_as_complete(self):
        today = self.operational_day
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(9, 0),
            Appointment.Status.PENDING,
        )

        almost_full_response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )
        slot_09 = self._slot_context(almost_full_response, "09:00")
        self.assertEqual(slot_09["complete_label"], "")

        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        full_response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )
        slot_09 = self._slot_context(full_response, "09:00")
        self.assertEqual(slot_09["complete_label"], "Completo")

    def test_daily_panel_marks_slot_as_complete_when_capacity_is_exhausted(self):
        today = self.operational_day
        self._create_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(9, 0),
            Appointment.Status.PENDING,
        )
        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.fourth_client,
            self.review_service,
            today,
            time(10, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        slot_09 = self._slot_context(response, "09:00")
        slot_10 = self._slot_context(response, "10:00")

        self.assertEqual(slot_09["complete_label"], "Completo")
        self.assertEqual(slot_10["complete_label"], "")
        self.assertContains(response, "Completo")
        self.assertNotContains(response, response.context["selected_day_summary"])


class SettingsIndexViewTests(AuthenticatedAgendaBaseTestCase):
    def test_settings_index_groups_agenda_availability_and_services(self):
        response = self.client.get(reverse("core:settings_index"))

        business_settings_url = reverse("core:business_settings")
        agenda_settings_url = reverse("core:agenda_settings")
        service_settings_url = reverse("core:service_settings")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/settings_index.html")
        self.assertContains(response, "Ajustes")
        self.assertContains(response, "Configura tu negocio, la agenda y los servicios desde un solo sitio.")
        self.assertContains(response, 'class="section-surface settings-index__group"', count=3)
        self.assertContains(response, "Datos del negocio")
        self.assertContains(response, "Configura la identidad basica del negocio que usa esta agenda.")
        self.assertContains(response, "Nombre, contacto y datos fiscales de la instancia actual.")
        self.assertContains(response, f'href="{business_settings_url}"')
        self.assertContains(response, "Agenda y disponibilidad")
        self.assertContains(response, "Gestiona horarios, cierres y dias no operativos.")
        self.assertContains(response, "Ajustes de agenda")
        self.assertContains(response, f'href="{agenda_settings_url}"')
        self.assertContains(response, f'href="{agenda_settings_url}#cierres-manuales"')
        self.assertContains(response, f'href="{agenda_settings_url}#festivos-oficiales"')
        self.assertContains(response, "Festivos sincronizados desde BOE en solo lectura.")
        self.assertContains(response, "Servicios")
        self.assertContains(response, "Define los servicios que puedes reservar en tu agenda.")
        self.assertContains(response, "Gestiona el catalogo operativo de servicios.")
        self.assertContains(response, f'href="{service_settings_url}"')
        self.assertNotContains(response, 'class="settings-breadcrumbs"')
        self.assertNotContains(response, "/app/ui/")
        self.assertNotContains(response, "/app/calendar-ui/")

    def test_settings_index_renders_vertical_stack_in_expected_order(self):
        response = self.client.get(reverse("core:settings_index"))
        content = response.content.decode()

        business_index = content.index("<h2>Datos del negocio</h2>")
        agenda_index = content.index("<h2>Agenda y disponibilidad</h2>")
        services_index = content.index("<h2>Servicios</h2>")

        self.assertContains(response, 'class="settings-index__stack"')
        self.assertLess(business_index, agenda_index)
        self.assertLess(agenda_index, services_index)


class BusinessSettingsViewTests(AuthenticatedAgendaBaseTestCase):
    def test_business_settings_page_renders_single_business_surface(self):
        response = self.client.get(reverse("core:business_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/business_settings.html")
        self.assertEqual(
            response.context["settings_breadcrumbs"],
            [
                {"label": "Ajustes", "url": reverse("core:settings_index")},
                {"label": "Datos del negocio", "url": ""},
            ],
        )
        self.assertContains(response, 'class="settings-breadcrumbs"')
        self.assertContains(response, "Guarda aquí los datos principales de tu negocio.")
        self.assertContains(response, "Datos principales")
        self.assertContains(response, "Nombre, contacto y dirección en una sola ficha.")
        self.assertContains(response, 'name="business_name"')
        self.assertContains(response, 'name="phone"')
        self.assertContains(response, 'name="email"')
        self.assertContains(response, 'name="address"')
        self.assertContains(response, 'name="city"')
        self.assertContains(response, 'name="tax_id"')
        self.assertContains(response, "Guardar cambios")

    def test_business_settings_page_renders_fields_in_expected_visual_order(self):
        response = self.client.get(reverse("core:business_settings"))
        content = response.content.decode()

        business_name_index = content.index('name="business_name"')
        phone_index = content.index('name="phone"')
        email_index = content.index('name="email"')
        tax_id_index = content.index('name="tax_id"')
        address_index = content.index('name="address"')
        city_index = content.index('name="city"')

        self.assertLess(business_name_index, phone_index)
        self.assertLess(phone_index, email_index)
        self.assertLess(email_index, tax_id_index)
        self.assertLess(tax_id_index, address_index)
        self.assertLess(address_index, city_index)

    def test_business_settings_page_creates_initial_singleton_when_missing(self):
        self.assertEqual(BusinessSettings.objects.count(), 0)

        response = self.client.post(
            reverse("core:business_settings"),
            {
                "business_name": "Clinica Atlas",
                "phone": "600 123 123",
                "email": "info@atlas.test",
                "address": "Calle Mayor 12",
                "city": "Madrid",
                "tax_id": "B12345678",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Datos del negocio guardados.")
        self.assertEqual(BusinessSettings.objects.count(), 1)

        business_settings = BusinessSettings.objects.get()
        self.assertEqual(business_settings.pk, 1)
        self.assertEqual(business_settings.business_name, "Clinica Atlas")
        self.assertEqual(business_settings.tax_id, "B12345678")

    def test_business_settings_page_edits_existing_singleton_without_creating_extra_records(self):
        business_settings = BusinessSettings.objects.create(
            business_name="Clinica Atlas",
            phone="600 123 123",
            email="info@atlas.test",
            address="Calle Mayor 12",
            city="Madrid",
            tax_id="B12345678",
        )

        response = self.client.post(
            reverse("core:business_settings"),
            {
                "business_name": "Centro Atlas Salud",
                "phone": "911 222 333",
                "email": "hola@atlas.test",
                "address": "Avenida Norte 8",
                "city": "Alcobendas",
                "tax_id": "B87654321",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Datos del negocio guardados.")
        self.assertEqual(BusinessSettings.objects.count(), 1)

        business_settings.refresh_from_db()
        self.assertEqual(business_settings.business_name, "Centro Atlas Salud")
        self.assertEqual(business_settings.phone, "911 222 333")
        self.assertEqual(business_settings.email, "hola@atlas.test")
        self.assertEqual(business_settings.address, "Avenida Norte 8")
        self.assertEqual(business_settings.city, "Alcobendas")
        self.assertEqual(business_settings.tax_id, "B87654321")


class AgendaSettingsViewTests(AuthenticatedAgendaBaseTestCase):
    def test_settings_page_creates_singleton_and_renders_empty_states(self):
        response = self.client.get(reverse("core:agenda_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/agenda_settings.html")
        self.assertEqual(
            response.context["settings_breadcrumbs"],
            [
                {"label": "Ajustes", "url": reverse("core:settings_index")},
                {"label": "Agenda y disponibilidad", "url": ""},
            ],
        )
        self.assertContains(response, 'class="settings-breadcrumbs"')
        self.assertContains(response, f'href="{reverse("core:settings_index")}">Ajustes</a>')
        self.assertContains(response, 'aria-current="page">Agenda y disponibilidad</span>')
        self.assertContains(response, "Ajustes de agenda")
        self.assertContains(response, "Reglas base de la agenda")
        self.assertContains(response, "parrilla fija de 8 tramos")
        self.assertContains(
            response,
            "Aqui decides si sabados, domingos y festivos oficiales bloquean o no la operativa base.",
        )
        self.assertContains(response, "Aplicar festivos oficiales como no operativos")
        self.assertContains(
            response,
            "Si lo desactivas, los festivos oficiales seguiran visibles como dato, pero no cerraran operativamente la agenda.",
        )
        self.assertContains(response, "Sin cierres")
        self.assertContains(
            response,
            "Gestiona cierres de un dia o de un rango completo por vacaciones, fiesta local, puente o cualquier otra necesidad del negocio.",
        )
        self.assertContains(response, "Sin festivos oficiales")
        self.assertContains(response, "Importar festivos nacionales desde BOE")
        self.assertContains(
            response,
            "Consulta solo los festivos oficiales sincronizados desde BOE. Para vacaciones, fiestas locales, puentes o cualquier otro cierre del negocio, usa Cierres manuales.",
        )
        self.assertContains(response, "Ultimo sync BOE completado")
        self.assertContains(response, "Todavia no hay una importacion BOE completada registrada en esta agenda.")
        self.assertContains(response, "Ultimo fallo de sync BOE")
        self.assertContains(response, "Todavia no hay un fallo de sync BOE registrado en esta agenda.")
        self.assertNotContains(response, "Limpiar ultimo fallo")
        self.assertNotContains(response, "Crear festivo")
        self.assertNotContains(response, "/app/settings/agenda/official-holidays/")
        self.assertContains(response, 'id="agenda-reglas"')
        self.assertContains(response, 'id="cierres-manuales"')
        self.assertContains(response, 'id="festivos-oficiales"')
        self.assertContains(response, 'name="year"')
        self.assertContains(response, reverse("core:manual_closure_create"))
        self.assertNotContains(response, "Volver a la agenda")
        self.assertEqual(AgendaSettings.objects.count(), 1)

    def test_settings_page_updates_global_singleton(self):
        response = self.client.post(
            reverse("core:agenda_settings"),
            {
                "saturdays_non_working": "on",
                "official_holidays_non_working": "on",
            },
        )

        self.assertRedirects(response, reverse("core:agenda_settings"), fetch_redirect_response=False)
        settings = AgendaSettings.get_solo()
        self.assertTrue(settings.saturdays_non_working)
        self.assertFalse(settings.sundays_non_working)
        self.assertTrue(settings.official_holidays_non_working)

    def test_settings_page_can_disable_operational_effect_of_official_holidays(self):
        response = self.client.post(
            reverse("core:agenda_settings"),
            {
                "saturdays_non_working": "on",
                "sundays_non_working": "on",
            },
        )

        self.assertRedirects(response, reverse("core:agenda_settings"), fetch_redirect_response=False)
        settings = AgendaSettings.get_solo()
        self.assertTrue(settings.saturdays_non_working)
        self.assertTrue(settings.sundays_non_working)
        self.assertFalse(settings.official_holidays_non_working)

    def test_settings_page_lists_existing_manual_closures_with_actions(self):
        manual_closure = ManualClosure.objects.create(
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 22),
            reason_type=ManualClosure.ReasonType.VACATION,
            label="Vacaciones de abril",
            notes="Cierre completo",
        )

        response = self.client.get(reverse("core:agenda_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vacaciones de abril")
        self.assertContains(response, "20/04/2026 a 22/04/2026")
        self.assertContains(response, reverse("core:manual_closure_update", args=[manual_closure.pk]))
        self.assertContains(response, reverse("core:manual_closure_delete", args=[manual_closure.pk]))

    def test_settings_page_lists_only_boe_holidays_and_keeps_manual_closure_management_visible(self):
        manual_closure = ManualClosure.objects.create(
            start_date=date(2026, 4, 24),
            end_date=date(2026, 4, 25),
            reason_type=ManualClosure.ReasonType.LOCAL_HOLIDAY,
            label="Fiesta local",
        )
        manual_holiday = OfficialHoliday.objects.create(
            day=date(2026, 4, 22),
            name="Vispera manual",
        )
        official_holiday = OfficialHoliday.objects.create(
            day=date(2026, 4, 23),
            name="San Jorge",
            source=OfficialHoliday.Source.BOE_NATIONAL_SYNC,
        )

        response = self.client.get(reverse("core:agenda_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Fiesta local")
        self.assertContains(response, reverse("core:manual_closure_update", args=[manual_closure.pk]))
        self.assertContains(response, reverse("core:manual_closure_delete", args=[manual_closure.pk]))
        self.assertNotContains(response, "Vispera manual")
        self.assertContains(response, "San Jorge")
        self.assertContains(response, "23/04/2026")
        self.assertContains(response, "Sync BOE nacional")
        self.assertNotContains(response, "/app/settings/agenda/official-holidays/")
        self.assertContains(response, "<th>Acciones</th>", count=1, html=True)

    def test_removed_manual_official_holiday_named_urls_are_not_registered(self):
        with self.assertRaises(NoReverseMatch):
            reverse("core:official_holiday_create")
        with self.assertRaises(NoReverseMatch):
            reverse("core:official_holiday_update", args=[1])
        with self.assertRaises(NoReverseMatch):
            reverse("core:official_holiday_delete", args=[1])

    def test_removed_manual_official_holiday_paths_return_404(self):
        official_holiday = OfficialHoliday.objects.create(day=date(2026, 4, 23), name="San Jorge")

        request_specs = [
            ("get", "/app/settings/agenda/official-holidays/new/", {}),
            ("post", "/app/settings/agenda/official-holidays/new/", {"day": "2026-04-24", "name": "Festivo"}),
            ("get", f"/app/settings/agenda/official-holidays/{official_holiday.pk}/edit/", {}),
            ("post", f"/app/settings/agenda/official-holidays/{official_holiday.pk}/edit/", {"day": "2026-04-24"}),
            ("get", f"/app/settings/agenda/official-holidays/{official_holiday.pk}/delete/", {}),
            ("post", f"/app/settings/agenda/official-holidays/{official_holiday.pk}/delete/", {}),
        ]

        for method_name, path, payload in request_specs:
            response = getattr(self.client, method_name)(path, payload)
            self.assertEqual(response.status_code, 404)

    @patch("core.views.import_boe_national_holidays")
    @patch("core.views.timezone.now")
    def test_settings_page_can_launch_manual_boe_sync_and_persist_visible_trace(self, mocked_now, mocked_import):
        mocked_now.return_value = timezone.make_aware(datetime(2026, 4, 9, 10, 45))
        mocked_import.return_value = OfficialHolidaySyncResult(
            target_year=2026,
            resolution=BoeHolidayResolution(
                identifier="BOE-A-2025-21667",
                title=(
                    "Resolución de 17 de octubre de 2025, de la Dirección General de Trabajo, "
                    "por la que se publica la relación de fiestas laborales para el año 2026."
                ),
                url_html="https://www.boe.es/diario_boe/txt.php?id=BOE-A-2025-21667",
            ),
            created_count=8,
            skipped_existing_count=1,
            error_count=0,
            reconciled_count=1,
        )

        response = self.client.post(
            reverse("core:agenda_settings"),
            {
                "agenda_action": "sync_official_holidays",
                "year": "2026",
            },
            follow=True,
        )

        mocked_import.assert_called_once_with(2026)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Importacion BOE 2026 completada.")
        self.assertContains(response, "Creados: 8.")
        self.assertContains(response, "Ignorados existentes: 1.")
        self.assertContains(response, "Reconciliados: 1.")
        self.assertContains(response, "Errores: 0.")

        settings = AgendaSettings.get_solo()
        self.assertEqual(settings.last_boe_sync_year, 2026)
        self.assertEqual(settings.last_boe_sync_resolution_identifier, "BOE-A-2025-21667")
        self.assertIn("relación de fiestas laborales para el año 2026", settings.last_boe_sync_resolution_title)
        self.assertEqual(
            settings.last_boe_sync_resolution_url,
            "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2025-21667",
        )
        self.assertEqual(settings.last_boe_sync_created_count, 8)
        self.assertEqual(settings.last_boe_sync_skipped_existing_count, 1)
        self.assertEqual(settings.last_boe_sync_error_count, 0)
        self.assertEqual(settings.last_boe_sync_at, mocked_now.return_value)

        self.assertContains(response, "Ultima actualizacion registrada el 09/04/2026 10:45.")
        self.assertContains(response, "Año importado")
        self.assertContains(response, "2026")
        self.assertContains(response, "BOE-A-2025-21667")
        self.assertContains(
            response,
            "Resolución de 17 de octubre de 2025, de la Dirección General de Trabajo, "
            "por la que se publica la relación de fiestas laborales para el año 2026.",
        )
        self.assertContains(response, "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2025-21667")

    @patch("core.views.import_boe_national_holidays")
    @patch("core.views.timezone.now")
    def test_settings_page_persists_and_shows_last_boe_sync_failure(self, mocked_now, mocked_import):
        mocked_now.return_value = timezone.make_aware(datetime(2026, 4, 9, 12, 5))
        mocked_import.side_effect = BoeSyncError("No se ha encontrado la resolución en el BOE.")

        response = self.client.post(
            reverse("core:agenda_settings"),
            {
                "agenda_action": "sync_official_holidays",
                "year": "2026",
            },
            follow=True,
        )

        mocked_import.assert_called_once_with(2026)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No se pudo completar la importacion BOE 2026")
        self.assertContains(response, "No se ha encontrado la resolución en el BOE.")

        settings = AgendaSettings.get_solo()
        self.assertIsNone(settings.last_boe_sync_year)
        self.assertEqual(settings.last_boe_sync_failure_at, mocked_now.return_value)
        self.assertEqual(settings.last_boe_sync_failure_year, 2026)
        self.assertEqual(settings.last_boe_sync_failure_message, "No se ha encontrado la resolución en el BOE.")

        self.assertContains(response, "Ultimo fallo de sync BOE")
        self.assertContains(response, "Ultimo intento fallido registrado el 09/04/2026 12:05.")
        self.assertContains(response, "Año intentado:")
        self.assertContains(response, "2026")
        self.assertContains(response, 'value="clear_sync_failure"')
        self.assertContains(response, "Limpiar ultimo fallo")

    @patch("core.views.import_boe_national_holidays")
    @patch("core.views.timezone.now")
    def test_settings_page_keeps_last_successful_trace_when_new_sync_fails(self, mocked_now, mocked_import):
        mocked_now.return_value = timezone.make_aware(datetime(2026, 4, 10, 9, 15))
        settings = AgendaSettings.get_solo()
        settings.last_boe_sync_at = timezone.make_aware(datetime(2026, 4, 8, 18, 30))
        settings.last_boe_sync_year = 2025
        settings.last_boe_sync_resolution_identifier = "BOE-A-2024-99999"
        settings.last_boe_sync_resolution_title = "Resolucion previa"
        settings.last_boe_sync_resolution_url = "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2024-99999"
        settings.last_boe_sync_created_count = 7
        settings.last_boe_sync_skipped_existing_count = 2
        settings.last_boe_sync_error_count = 0
        settings.save()

        mocked_import.side_effect = BoeSyncError("Fallo temporal")

        response = self.client.post(
            reverse("core:agenda_settings"),
            {
                "agenda_action": "sync_official_holidays",
                "year": "2026",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No se pudo completar la importacion BOE 2026")
        self.assertContains(response, "Ultimo sync BOE completado")
        self.assertContains(response, "BOE-A-2024-99999")
        self.assertContains(response, "Resolucion previa")
        self.assertContains(response, "Ultimo fallo de sync BOE")
        self.assertContains(response, "Ultimo intento fallido registrado el 10/04/2026 09:15.")
        self.assertContains(response, "Fallo temporal")

        settings.refresh_from_db()
        self.assertEqual(settings.last_boe_sync_year, 2025)
        self.assertEqual(settings.last_boe_sync_created_count, 7)
        self.assertEqual(settings.last_boe_sync_failure_at, mocked_now.return_value)
        self.assertEqual(settings.last_boe_sync_failure_year, 2026)
        self.assertEqual(settings.last_boe_sync_failure_message, "Fallo temporal")

    def test_settings_page_shows_clear_failure_action_only_when_failure_trace_exists(self):
        settings = AgendaSettings.get_solo()
        settings.last_boe_sync_failure_at = timezone.make_aware(datetime(2026, 4, 10, 9, 15))
        settings.last_boe_sync_failure_year = 2026
        settings.last_boe_sync_failure_message = "Fallo temporal"
        settings.save()

        response = self.client.get(reverse("core:agenda_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'value="clear_sync_failure"')
        self.assertContains(response, "Limpiar ultimo fallo")

    def test_settings_page_can_clear_last_boe_sync_failure_without_touching_last_success(self):
        settings = AgendaSettings.get_solo()
        success_at = timezone.make_aware(datetime(2026, 4, 9, 10, 45))
        failure_at = timezone.make_aware(datetime(2026, 4, 10, 9, 15))
        settings.last_boe_sync_at = success_at
        settings.last_boe_sync_year = 2026
        settings.last_boe_sync_resolution_identifier = "BOE-A-2025-21667"
        settings.last_boe_sync_resolution_title = "Resolucion correcta"
        settings.last_boe_sync_resolution_url = "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2025-21667"
        settings.last_boe_sync_created_count = 8
        settings.last_boe_sync_skipped_existing_count = 1
        settings.last_boe_sync_error_count = 0
        settings.last_boe_sync_failure_at = failure_at
        settings.last_boe_sync_failure_year = 2026
        settings.last_boe_sync_failure_message = "Fallo temporal"
        settings.save()

        response = self.client.post(
            reverse("core:agenda_settings"),
            {
                "agenda_action": "clear_sync_failure",
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Traza del ultimo fallo BOE limpiada.")
        self.assertContains(response, "Ultimo sync BOE completado")
        self.assertContains(response, "BOE-A-2025-21667")
        self.assertContains(response, "Todavia no hay un fallo de sync BOE registrado en esta agenda.")
        self.assertNotContains(response, "Fallo temporal")
        self.assertNotContains(response, "Limpiar ultimo fallo")

        settings.refresh_from_db()
        self.assertEqual(settings.last_boe_sync_at, success_at)
        self.assertEqual(settings.last_boe_sync_year, 2026)
        self.assertEqual(settings.last_boe_sync_resolution_identifier, "BOE-A-2025-21667")
        self.assertEqual(settings.last_boe_sync_created_count, 8)
        self.assertIsNone(settings.last_boe_sync_failure_at)
        self.assertIsNone(settings.last_boe_sync_failure_year)
        self.assertEqual(settings.last_boe_sync_failure_message, "")

    def test_settings_page_surfaces_sync_form_validation_errors(self):
        response = self.client.post(
            reverse("core:agenda_settings"),
            {
                "agenda_action": "sync_official_holidays",
                "year": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("year", response.context["sync_form"].errors)


class ServiceSettingsViewTests(AuthenticatedAgendaBaseTestCase):
    def test_service_settings_returns_200_and_lists_operational_services(self):
        self.control_service.is_active = False
        self.control_service.save(update_fields=["is_active"])

        response = self.client.get(reverse("core:service_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/service_settings.html")
        self.assertEqual(response.context["back_url"], reverse("core:settings_index"))
        self.assertEqual(list(response.context["services"]), [self.review_service])
        self.assertEqual(
            response.context["settings_breadcrumbs"],
            [
                {"label": "Ajustes", "url": reverse("core:settings_index")},
                {"label": "Servicios", "url": ""},
            ],
        )
        self.assertContains(response, 'class="settings-breadcrumbs"')
        self.assertContains(response, f'href="{reverse("core:settings_index")}">Ajustes</a>')
        self.assertContains(response, 'aria-current="page">Servicios</span>')
        self.assertContains(response, "Servicios")
        self.assertContains(response, "Define los servicios que puedes reservar en tu agenda.")
        self.assertContains(response, "Catalogo operativo")
        self.assertContains(response, "Nuevo servicio")
        self.assertContains(response, self.review_service.name)
        self.assertContains(response, reverse("core:service_create"))
        self.assertContains(response, reverse("core:service_update", args=[self.review_service.pk]))
        self.assertContains(response, reverse("core:service_delete", args=[self.review_service.pk]))
        self.assertNotContains(response, self.control_service.name)
        self.assertNotContains(response, "Duracion")
        self.assertNotContains(response, "Color")
        self.assertNotContains(response, self.review_service.color)
        self.assertContains(response, reverse("core:settings_index"))
        self.assertNotContains(response, "Volver a Ajustes")

    def test_service_settings_renders_empty_state_when_no_active_services(self):
        Service.objects.update(is_active=False)

        response = self.client.get(reverse("core:service_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sin servicios")
        self.assertContains(response, "Todavia no hay servicios disponibles para reservar en la agenda.")
        self.assertContains(response, "Crear primer servicio")

    def test_service_create_view_renders_minimal_form(self):
        response = self.client.get(reverse("core:service_create"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/service_form.html")
        self.assertEqual(
            response.context["settings_breadcrumbs"],
            [
                {"label": "Ajustes", "url": reverse("core:settings_index")},
                {"label": "Servicios", "url": reverse("core:service_settings")},
                {"label": "Nuevo servicio", "url": ""},
            ],
        )
        self.assertContains(response, "Nuevo servicio")
        self.assertContains(response, "Nombre")
        self.assertContains(response, "Descripcion")
        self.assertNotContains(response, 'name="duration_minutes"')
        self.assertNotContains(response, 'name="color"')

    def test_service_create_view_creates_active_service(self):
        response = self.client.post(
            reverse("core:service_create"),
            {
                "name": "Primera consulta",
                "description": "Sesion inicial para valorar el caso.",
            },
        )

        self.assertRedirects(response, reverse("core:service_settings"), fetch_redirect_response=False)
        service = Service.objects.get(name="Primera consulta")
        self.assertEqual(service.description, "Sesion inicial para valorar el caso.")
        self.assertTrue(service.is_active)
        self.assertEqual(service.color, "")

    def test_service_update_view_updates_name_and_description(self):
        response = self.client.post(
            reverse("core:service_update", args=[self.review_service.pk]),
            {
                "name": "Revision completa",
                "description": "Seguimiento general.",
            },
        )

        self.assertRedirects(response, reverse("core:service_settings"), fetch_redirect_response=False)
        self.review_service.refresh_from_db()
        self.assertEqual(self.review_service.name, "Revision completa")
        self.assertEqual(self.review_service.description, "Seguimiento general.")
        self.assertTrue(self.review_service.is_active)

    def test_service_delete_view_marks_service_inactive_without_breaking_history(self):
        appointment = self._create_appointment(
            self.primary_client,
            self.review_service,
            self.operational_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )

        confirm_response = self.client.get(reverse("core:service_delete", args=[self.review_service.pk]))

        self.assertEqual(confirm_response.status_code, 200)
        self.assertTemplateUsed(confirm_response, "core/service_confirm_delete.html")
        self.assertContains(confirm_response, "Eliminar del catalogo")
        self.assertContains(confirm_response, "Las citas existentes conservaran esta referencia.")

        response = self.client.post(reverse("core:service_delete", args=[self.review_service.pk]))

        self.assertRedirects(response, reverse("core:service_settings"), fetch_redirect_response=False)
        self.review_service.refresh_from_db()
        appointment.refresh_from_db()
        self.assertFalse(self.review_service.is_active)
        self.assertEqual(self._appointment_service_ids(appointment), [self.review_service.pk])
        self.assertTrue(Service.objects.filter(pk=self.review_service.pk).exists())

        detail_response = self.client.get(reverse("core:client_detail", args=[self.primary_client.pk]))
        self.assertContains(detail_response, self.review_service.name)

    def test_deleted_service_is_hidden_for_new_appointments_but_available_to_existing_edits(self):
        appointment = self._create_appointment(
            self.primary_client,
            self.control_service,
            self.operational_day,
            time(9, 0),
            Appointment.Status.CONFIRMED,
        )
        self.control_service.is_active = False
        self.control_service.save(update_fields=["is_active"])

        new_form = AppointmentForm()
        edit_form = AppointmentForm(instance=appointment)

        self.assertNotIn(self.control_service, list(new_form.fields["services"].queryset))
        self.assertIn(self.control_service, list(edit_form.fields["services"].queryset))


class ManualClosureViewTests(AuthenticatedAgendaBaseTestCase):
    def test_create_view_renders_form(self):
        response = self.client.get(reverse("core:manual_closure_create"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/manual_closure_form.html")
        self.assertContains(response, "Nuevo cierre manual")
        self.assertContains(response, "Fecha inicial")

    def test_create_view_creates_manual_closure_and_redirects_to_settings(self):
        response = self.client.post(
            reverse("core:manual_closure_create"),
            {
                "start_date": "2026-04-20",
                "end_date": "2026-04-22",
                "reason_type": ManualClosure.ReasonType.VACATION,
                "label": "Vacaciones de abril",
                "notes": "Semana cerrada",
            },
        )

        self.assertRedirects(response, reverse("core:agenda_settings"), fetch_redirect_response=False)
        self.assertTrue(
            ManualClosure.objects.filter(
                start_date=date(2026, 4, 20),
                end_date=date(2026, 4, 22),
                label="Vacaciones de abril",
            ).exists()
        )

    def test_create_view_surfaces_model_validation_errors(self):
        ManualClosure.objects.create(
            start_date=date(2026, 4, 14),
            end_date=date(2026, 4, 16),
            reason_type=ManualClosure.ReasonType.BUSINESS_CLOSURE,
        )

        response = self.client.post(
            reverse("core:manual_closure_create"),
            {
                "start_date": "2026-04-16",
                "end_date": "2026-04-18",
                "reason_type": ManualClosure.ReasonType.OTHER,
                "label": "Otro cierre",
                "notes": "",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "solapa")
        self.assertEqual(ManualClosure.objects.count(), 1)

    def test_update_view_updates_manual_closure(self):
        manual_closure = ManualClosure.objects.create(
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 22),
            reason_type=ManualClosure.ReasonType.VACATION,
            label="Vacaciones de abril",
        )

        response = self.client.post(
            reverse("core:manual_closure_update", args=[manual_closure.pk]),
            {
                "start_date": "2026-04-20",
                "end_date": "2026-04-23",
                "reason_type": ManualClosure.ReasonType.BUSINESS_CLOSURE,
                "label": "Cierre de semana",
                "notes": "Actualizado",
            },
        )

        self.assertRedirects(response, reverse("core:agenda_settings"), fetch_redirect_response=False)
        manual_closure.refresh_from_db()
        self.assertEqual(manual_closure.end_date, date(2026, 4, 23))
        self.assertEqual(manual_closure.reason_type, ManualClosure.ReasonType.BUSINESS_CLOSURE)
        self.assertEqual(manual_closure.label, "Cierre de semana")
        self.assertEqual(manual_closure.notes, "Actualizado")

    def test_delete_view_confirms_and_deletes_manual_closure(self):
        manual_closure = ManualClosure.objects.create(
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            reason_type=ManualClosure.ReasonType.PERSONAL,
            label="Asunto personal",
        )

        get_response = self.client.get(reverse("core:manual_closure_delete", args=[manual_closure.pk]))
        self.assertEqual(get_response.status_code, 200)
        self.assertTemplateUsed(get_response, "core/manual_closure_confirm_delete.html")
        self.assertContains(get_response, "Confirmar eliminacion")

        post_response = self.client.post(reverse("core:manual_closure_delete", args=[manual_closure.pk]))
        self.assertRedirects(post_response, reverse("core:agenda_settings"), fetch_redirect_response=False)
        self.assertFalse(ManualClosure.objects.filter(pk=manual_closure.pk).exists())


class OfficialHolidaySyncCommandTests(TestCase):
    def _resolution(self, year=2026):
        return BoeHolidayResolution(
            identifier=f"BOE-A-{year - 1}-21667",
            title=(
                f"Resolución de 17 de octubre de {year - 1}, de la Dirección General de Trabajo,"
                f" por la que se publica la relación de fiestas laborales para el año {year}."
            ),
            url_html=f"https://www.boe.es/diario_boe/txt.php?id=BOE-A-{year - 1}-21667",
        )

    def test_command_imports_new_holidays_with_boe_source(self):
        stdout = StringIO()

        with patch(
            "core.management.commands.sync_official_holidays.BoeNationalHolidaySyncService.fetch_national_holidays",
            return_value=(
                self._resolution(),
                [
                    OfficialHolidayImport(day=date(2026, 1, 1), name="Año Nuevo"),
                    OfficialHolidayImport(day=date(2026, 4, 3), name="Viernes Santo"),
                ],
            ),
        ):
            call_command("sync_official_holidays", "--year", "2026", stdout=stdout)

        self.assertEqual(OfficialHoliday.objects.count(), 2)
        self.assertTrue(
            OfficialHoliday.objects.filter(
                day=date(2026, 1, 1),
                name="Año Nuevo",
                source=OfficialHoliday.Source.BOE_NATIONAL_SYNC,
            ).exists()
        )
        self.assertIn("creados=2", stdout.getvalue())
        self.assertIn("ignorados_existentes=0", stdout.getvalue())
        self.assertIn("reconciliados=0", stdout.getvalue())

    def test_command_ignores_existing_manual_holiday_without_overwriting_it(self):
        manual_holiday = OfficialHoliday.objects.create(
            day=date(2026, 1, 1),
            name="Nombre manual",
        )
        stdout = StringIO()

        with patch(
            "core.management.commands.sync_official_holidays.BoeNationalHolidaySyncService.fetch_national_holidays",
            return_value=(
                self._resolution(),
                [
                    OfficialHolidayImport(day=date(2026, 1, 1), name="Año Nuevo"),
                    OfficialHolidayImport(day=date(2026, 4, 3), name="Viernes Santo"),
                ],
            ),
        ):
            call_command("sync_official_holidays", "--year", "2026", stdout=stdout)

        manual_holiday.refresh_from_db()
        self.assertEqual(manual_holiday.name, "Nombre manual")
        self.assertEqual(manual_holiday.source, OfficialHoliday.Source.MANUAL)
        self.assertEqual(OfficialHoliday.objects.count(), 2)
        self.assertIn("creados=1", stdout.getvalue())
        self.assertIn("ignorados_existentes=1", stdout.getvalue())
        self.assertIn("reconciliados=0", stdout.getvalue())

    def test_command_does_not_duplicate_existing_synced_holiday(self):
        OfficialHoliday.objects.create(
            day=date(2026, 1, 1),
            name="Año Nuevo",
            source=OfficialHoliday.Source.BOE_NATIONAL_SYNC,
        )
        stdout = StringIO()

        with patch(
            "core.management.commands.sync_official_holidays.BoeNationalHolidaySyncService.fetch_national_holidays",
            return_value=(
                self._resolution(),
                [
                    OfficialHolidayImport(day=date(2026, 1, 1), name="Año Nuevo"),
                ],
            ),
        ):
            call_command("sync_official_holidays", "--year", "2026", stdout=stdout)

        self.assertEqual(OfficialHoliday.objects.count(), 1)
        self.assertIn("creados=0", stdout.getvalue())
        self.assertIn("ignorados_existentes=1", stdout.getvalue())
        self.assertIn("reconciliados=0", stdout.getvalue())

    def test_command_reconciles_outdated_and_misaligned_boe_holidays_for_target_year(self):
        OfficialHoliday.objects.create(
            day=date(2026, 1, 1),
            name="Año Nuevo",
            source=OfficialHoliday.Source.BOE_NATIONAL_SYNC,
        )
        renamed_boe_holiday = OfficialHoliday.objects.create(
            day=date(2026, 5, 1),
            name="Nombre desalineado",
            source=OfficialHoliday.Source.BOE_NATIONAL_SYNC,
        )
        outdated_boe_holiday = OfficialHoliday.objects.create(
            day=date(2026, 4, 23),
            name="San Jorge",
            source=OfficialHoliday.Source.BOE_NATIONAL_SYNC,
        )
        next_year_holiday = OfficialHoliday.objects.create(
            day=date(2027, 1, 1),
            name="Año Nuevo 2027",
            source=OfficialHoliday.Source.BOE_NATIONAL_SYNC,
        )
        stdout = StringIO()

        with patch(
            "core.management.commands.sync_official_holidays.BoeNationalHolidaySyncService.fetch_national_holidays",
            return_value=(
                self._resolution(),
                [
                    OfficialHolidayImport(day=date(2026, 1, 1), name="Año Nuevo"),
                    OfficialHolidayImport(day=date(2026, 5, 1), name="Fiesta del Trabajo"),
                ],
            ),
        ):
            call_command("sync_official_holidays", "--year", "2026", stdout=stdout)

        renamed_boe_holiday.refresh_from_db()
        next_year_holiday.refresh_from_db()
        self.assertEqual(renamed_boe_holiday.name, "Fiesta del Trabajo")
        self.assertFalse(OfficialHoliday.objects.filter(pk=outdated_boe_holiday.pk).exists())
        self.assertTrue(OfficialHoliday.objects.filter(pk=next_year_holiday.pk).exists())
        self.assertEqual(OfficialHoliday.objects.filter(source=OfficialHoliday.Source.BOE_NATIONAL_SYNC).count(), 3)
        self.assertIn("creados=0", stdout.getvalue())
        self.assertIn("ignorados_existentes=1", stdout.getvalue())
        self.assertIn("reconciliados=2", stdout.getvalue())
        self.assertIn("errores=0", stdout.getvalue())

    def test_command_raises_clear_error_when_boe_lookup_fails(self):
        with patch(
            "core.management.commands.sync_official_holidays.BoeNationalHolidaySyncService.fetch_national_holidays",
            side_effect=BoeSyncError("No se ha encontrado la resolución en el BOE."),
        ):
            with self.assertRaises(CommandError) as raised:
                call_command("sync_official_holidays", "--year", "2026")

        self.assertIn("No se ha encontrado la resolución", str(raised.exception))


class SeedAgendaDemoCommandTests(TestCase):
    @patch("core.management.commands.seed_agenda_demo.timezone.localdate", return_value=date(2026, 4, 9))
    def test_command_uses_only_working_days_for_seeded_appointments(self, _mocked_localdate):
        stdout = StringIO()

        call_command("seed_agenda_demo", stdout=stdout)

        self.assertFalse(Appointment.objects.filter(start_at__date=date(2026, 4, 11)).exists())
        self.assertFalse(Appointment.objects.filter(start_at__date=date(2026, 4, 12)).exists())
        for appointment in Appointment.objects.all():
            resolved_day = DayAvailabilityResolver.resolve_for_global_agenda(appointment.slot_day)
            self.assertTrue(resolved_day.is_working_day)
