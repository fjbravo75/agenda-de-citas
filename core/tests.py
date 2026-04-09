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
from .forms import AgendaSettingsForm, ManualClosureForm, OfficialHolidaySyncForm
from .day_availability import DayAvailabilityResolver
from .models import (
    AgendaSettings,
    Appointment,
    AvailabilityBlock,
    Client,
    ManualClosure,
    OfficialHoliday,
    Service,
    agenda_slot_operational_state_map,
)


class AgendaBaseTestCase(TestCase):
    def setUp(self):
        super().setUp()
        self.review_service = Service.objects.create(name="Revision", duration_minutes=45, color="#3158D7")
        self.control_service = Service.objects.create(name="Control", duration_minutes=30, color="#2E7A58")
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
            service=service,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=service.duration_minutes),
            status=status,
        )

    def _create_appointment(self, client, service, target_day, start_time, status):
        appointment = self._build_appointment(client, service, target_day, start_time, status)
        appointment.save()
        return appointment

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
        base_day = start_day or timezone.localdate()
        delta = (weekday - base_day.weekday()) % 7
        if delta == 0 and not include_today:
            delta = 7
        return base_day + timedelta(days=delta)


class AuthenticatedAgendaBaseTestCase(AgendaBaseTestCase):
    def setUp(self):
        super().setUp()
        self.login_app_user()


class AppointmentSlotValidationTests(AgendaBaseTestCase):
    def test_operational_day_without_weekly_availability_uses_base_capacity_for_all_slots(self):
        today = timezone.localdate()

        slot_state_map = agenda_slot_operational_state_map(today)

        self.assertEqual(len(slot_state_map), 8)
        self.assertTrue(all(state["capacity"] == 3 for state in slot_state_map.values()))
        self.assertTrue(all(state["can_book"] for state in slot_state_map.values()))

    def test_valid_appointment_in_available_slot_can_be_created(self):
        today = timezone.localdate()

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
        today = timezone.localdate()

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

    def test_long_service_duration_does_not_block_next_slot_when_capacity_exists(self):
        today = timezone.localdate()
        self.review_service.duration_minutes = 120
        self.review_service.save(update_fields=["duration_minutes"])

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

        self.assertGreater(first_appointment.end_at, second_appointment.start_at)
        self.assertEqual(Appointment.objects.count(), 2)
        self.assertEqual(Appointment.active_slot_appointments_count(today, "09:00"), 1)
        self.assertEqual(Appointment.active_slot_appointments_count(today, "10:00"), 1)

    def test_appointment_in_blocked_slot_is_rejected(self):
        today = timezone.localdate()
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
        today = timezone.localdate()

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
        today = timezone.localdate()

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
        today = timezone.localdate()
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
        today = timezone.localdate()

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


class OfficialHolidaySyncFormTests(TestCase):
    def test_form_requires_a_valid_year(self):
        form = OfficialHolidaySyncForm(data={"year": ""})

        self.assertFalse(form.is_valid())
        self.assertIn("year", form.errors)


class AppAuthenticationBoundaryTests(AgendaBaseTestCase):
    def _assert_redirects_to_login(self, response, next_url):
        expected_url = f"{reverse('wagtailadmin_login')}?{urlencode({'next': next_url})}"
        self.assertRedirects(response, expected_url, fetch_redirect_response=False)

    def test_app_entrypoint_redirects_anonymous_user_to_wagtail_login(self):
        today = timezone.localdate()
        query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        requested_url = f"{reverse('core:app_entrypoint')}?{query}"

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self._assert_redirects_to_login(response, requested_url)

    def test_appointment_create_redirects_anonymous_user_to_wagtail_login(self):
        today = timezone.localdate()
        query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        requested_url = f"{reverse('core:appointment_create')}?{query}"

        response = self.client.get(
            reverse("core:appointment_create"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self._assert_redirects_to_login(response, requested_url)

    def test_client_create_redirects_anonymous_user_to_wagtail_login(self):
        today = timezone.localdate()
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

    def test_appointment_update_redirects_anonymous_user_to_wagtail_login(self):
        today = timezone.localdate()
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

    def test_client_detail_redirects_anonymous_user_to_wagtail_login(self):
        requested_url = reverse("core:client_detail", args=[self.primary_client.pk])

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_ui_preview_redirects_anonymous_user_to_wagtail_login(self):
        requested_url = reverse("core:ui_preview")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_calendar_ui_preview_redirects_anonymous_user_to_wagtail_login(self):
        requested_url = reverse("core:calendar_ui_preview")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_agenda_settings_redirects_anonymous_user_to_wagtail_login(self):
        requested_url = reverse("core:agenda_settings")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_manual_closure_create_redirects_anonymous_user_to_wagtail_login(self):
        requested_url = reverse("core:manual_closure_create")

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_manual_closure_update_redirects_anonymous_user_to_wagtail_login(self):
        manual_closure = ManualClosure.objects.create(
            start_date=timezone.localdate(),
            end_date=timezone.localdate(),
            reason_type=ManualClosure.ReasonType.OTHER,
        )
        requested_url = reverse("core:manual_closure_update", args=[manual_closure.pk])

        response = self.client.get(requested_url)

        self._assert_redirects_to_login(response, requested_url)

    def test_manual_closure_delete_redirects_anonymous_user_to_wagtail_login(self):
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
    def test_login_page_uses_custom_branding_and_preserves_next(self):
        app_url = reverse("core:app_entrypoint")

        response = self.client.get(reverse("wagtailadmin_login"), {"next": app_url})

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "wagtailadmin/login.html")
        self.assertContains(response, "Agenda de Citas")
        self.assertContains(response, "Accede a la agenda")
        self.assertContains(response, "Acceso - Agenda de Citas")
        self.assertContains(response, 'value="/app/"')
        self.assertContains(response, "Entrar")

    def test_login_page_post_redirects_to_app_when_next_is_provided(self):
        response = self.client.post(
            reverse("wagtailadmin_login"),
            {
                "username": self.app_user.username,
                "password": "agenda-pass-123",
                "next": reverse("core:app_entrypoint"),
            },
        )

        self.assertRedirects(response, reverse("core:app_entrypoint"))
        self.assertIn("_auth_user_id", self.client.session)

    def test_authenticated_app_shell_shows_current_user_and_logout_action(self):
        self.login_app_user()

        response = self.client.get(reverse("core:app_entrypoint"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.app_user.username)
        self.assertContains(response, reverse("core:app_logout"))
        self.assertContains(response, "Cerrar sesion")

    def test_app_logout_redirects_to_app_oriented_login_and_closes_session(self):
        self.login_app_user()

        response = self.client.post(reverse("core:app_logout"))

        expected_login_url = f"{reverse('wagtailadmin_login')}?{urlencode({'next': reverse('core:app_entrypoint')})}"
        self.assertRedirects(response, expected_login_url, fetch_redirect_response=False)
        self.assertNotIn("_auth_user_id", self.client.session)

        app_response = self.client.get(reverse("core:app_entrypoint"))
        self.assertRedirects(app_response, expected_login_url, fetch_redirect_response=False)

    def test_login_after_app_logout_returns_user_to_app_instead_of_admin(self):
        self.login_app_user()

        logout_response = self.client.post(reverse("core:app_logout"))
        expected_login_url = f"{reverse('wagtailadmin_login')}?{urlencode({'next': reverse('core:app_entrypoint')})}"

        self.assertRedirects(logout_response, expected_login_url, fetch_redirect_response=False)

        login_page = self.client.get(logout_response.headers["Location"])
        self.assertEqual(login_page.status_code, 200)
        self.assertContains(login_page, 'value="/app/"')

        login_response = self.client.post(
            reverse("wagtailadmin_login"),
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
        today = timezone.localdate()
        data = {
            "client": self.primary_client.pk,
            "service": self.review_service.pk,
            "day": today.isoformat(),
            "slot_time": "11:00",
            "status": Appointment.Status.PENDING,
            "internal_notes": "Nota breve",
        }
        data.update(overrides)
        return data

    def test_create_view_can_create_a_valid_appointment(self):
        today = timezone.localdate()

        response = self.client.post(reverse("core:appointment_create"), self._appointment_form_data())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Appointment.objects.count(), 1)
        appointment = Appointment.objects.get()
        self.assertEqual(appointment.slot_day, today)
        self.assertEqual(appointment.slot_time, "11:00")

    def test_create_view_can_create_a_valid_appointment_without_weekly_availability(self):
        today = timezone.localdate()

        response = self.client.post(reverse("core:appointment_create"), self._appointment_form_data())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Appointment.objects.count(), 1)
        appointment = Appointment.objects.get()
        self.assertEqual(appointment.slot_day, today)
        self.assertEqual(appointment.slot_time, "11:00")

    def test_create_view_keeps_slot_based_validation_even_when_another_service_runs_long(self):
        today = timezone.localdate()
        self.review_service.duration_minutes = 120
        self.review_service.save(update_fields=["duration_minutes"])
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
        self.assertGreater(first_appointment.end_at, created_appointment.start_at)

    def test_create_view_uses_agenda_layout_structure_for_new_screen(self):
        today = timezone.localdate()
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
        today = timezone.localdate()

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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        self.assertIn("10:00", response.context["appointment_context_label"])
        self.assertIn(str(today.day), response.context["appointment_context_label"])
        self.assertContains(response, "Nuevo cliente")
        self.assertContains(response, "Volver a Nueva cita")

    def test_client_create_view_creates_minimal_client_and_returns_to_new_appointment_preselected(self):
        today = timezone.localdate()
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

    def test_create_view_does_not_offer_cancelled_status(self):
        today = timezone.localdate()

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
        today = timezone.localdate()

        response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(status=Appointment.Status.CANCELLED),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Appointment.objects.count(), 0)
        self.assertIn("status", response.context["form"].errors)
        self.assertContains(response, "cancelled is not one of the available choices")

    def test_create_view_renders_non_bookable_slots_as_inert_and_keeps_bound_selection_on_invalid_post(self):
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
        self._create_block(today, "11:00", label="Bloqueo interno")

        response = self.client.post(reverse("core:appointment_create"), self._appointment_form_data())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bloqueado")
        self.assertEqual(Appointment.objects.count(), 0)

    def test_create_view_uses_base_capacity_without_weekly_availability(self):
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
                service=appointment.service_id,
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
                service=appointment.service_id,
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

    def test_update_view_shows_cancellation_notice_when_appointment_is_cancelled(self):
        today = timezone.localdate()
        appointment = self._create_existing_cancelled_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
        )

        response = self.client.get(reverse("core:appointment_update", args=[appointment.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "volvera a quedar libre")
        self.assertContains(response, "La cancelacion seguira registrada en la base de datos.")
        self.assertContains(response, 'data-cancel-notice')
        self.assertContains(response, 'data-delete-confirmation')
        self.assertRegex(response.content.decode(), r'data-delete-confirmation[^>]*hidden')

    def test_update_view_can_cancel_appointment_without_deleting_and_free_slot_for_new_booking(self):
        today = timezone.localdate()
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
                service=appointment.service_id,
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
        today = timezone.localdate()
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
                service=appointment.service_id,
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
        self.assertContains(history_response, reverse("core:appointment_update", args=[appointment.pk]))

    def test_update_view_rejects_manipulated_cancelled_appointment_in_blocked_slot(self):
        today = timezone.localdate()
        self._create_block(today, "10:00", label="Bloqueo interno")
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
                service=appointment.service_id,
                day=today.isoformat(),
                slot_time="10:00",
                status=Appointment.Status.CANCELLED,
                internal_notes="Intento manipulado a tramo bloqueado",
                delete_mode="false",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bloqueado")
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CANCELLED)
        self.assertEqual(appointment.slot_time, "09:00")

    def test_update_view_allows_default_operational_slot_without_weekly_availability(self):
        today = timezone.localdate()
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
                service=appointment.service_id,
                day=today.isoformat(),
                slot_time="10:00",
                status=Appointment.Status.CANCELLED,
                internal_notes="Reubicacion valida dentro de la parrilla base",
                delete_mode="false",
            ),
        )

        self.assertEqual(response.status_code, 302)
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CANCELLED)
        self.assertEqual(appointment.slot_time, "10:00")

    def test_update_view_rejects_manipulated_cancelled_appointment_in_complete_slot(self):
        today = timezone.localdate()
        appointment = self._create_existing_cancelled_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(9, 0),
        )
        self._create_appointment(
            self.secondary_client,
            self.control_service,
            today,
            time(10, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(10, 0),
            Appointment.Status.PENDING,
        )
        self._create_appointment(
            self.fourth_client,
            self.control_service,
            today,
            time(10, 0),
            Appointment.Status.CONFIRMED,
        )

        response = self.client.post(
            reverse("core:appointment_update", args=[appointment.pk]),
            self._appointment_form_data(
                client=appointment.client_id,
                service=appointment.service_id,
                day=today.isoformat(),
                slot_time="10:00",
                status=Appointment.Status.CANCELLED,
                internal_notes="Intento manipulado a tramo completo",
                delete_mode="false",
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "capacidad maxima")
        appointment.refresh_from_db()
        self.assertEqual(appointment.status, Appointment.Status.CANCELLED)
        self.assertEqual(appointment.slot_time, "09:00")

    def test_update_view_requires_explicit_delete_confirmation_and_then_removes_appointment(self):
        today = timezone.localdate()
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
                service=appointment.service_id,
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
                service=appointment.service_id,
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

    def test_client_detail_view_renders_history_in_descending_order_with_active_and_cancelled_appointments(self):
        today = timezone.localdate()
        previous_day = today - timedelta(days=1)
        evaluation_service = Service.objects.create(name="Evaluacion", duration_minutes=60, color="#AE4C42")

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
        self.assertEqual(
            history_urls,
            [
                f"{reverse('core:appointment_update', args=[newest_appointment.pk])}"
                f"?{urlencode({'year': today.year, 'month': today.month, 'day': today.day})}",
                f"{reverse('core:appointment_update', args=[middle_appointment.pk])}"
                f"?{urlencode({'year': today.year, 'month': today.month, 'day': today.day})}",
                f"{reverse('core:appointment_update', args=[oldest_appointment.pk])}"
                f"?{urlencode({'year': previous_day.year, 'month': previous_day.month, 'day': previous_day.day})}",
            ],
        )
        self.assertLess(content.index("Evaluacion"), content.index("Control"))
        self.assertLess(content.index("Control"), content.index("Revision"))

    def test_client_detail_view_uses_next_for_back_link(self):
        today = timezone.localdate()
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
        today = timezone.localdate()
        self._create_block(today, "16:00", label="Bloqueo interno")
        first_appointment = self._create_appointment(
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
            self.primary_client,
            self.review_service,
            today,
            time(12, 0),
            Appointment.Status.CONFIRMED,
        )
        self._create_existing_cancelled_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(17, 0),
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )
        slot_11 = self._slot_context(response, "11:00")
        slot_13 = self._slot_context(response, "13:00")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Claudia Real")
        self.assertContains(response, "10:00")
        self.assertContains(response, "Bloqueo interno")
        self.assertEqual(slot_11["available_label"], "Disponible")
        self.assertEqual(slot_13["available_label"], "Disponible")
        self.assertEqual(slot_13["unavailable_label"], "")
        self.assertNotContains(response, "Nora Real")
        self.assertContains(response, reverse("core:appointment_create"))
        self.assertContains(response, reverse("core:appointment_update", args=[first_appointment.pk]))
        self.assertContains(response, "pendientes y confirmadas del día")
        self.assertContains(response, "tramos ocupados del día")
        self.assertContains(response, "sin ocupar tramo activo")
        self.assertNotContains(response, response.context["selected_day_summary"])
        self.assertNotContains(response, "pending + confirmed del dia")

        metrics = {metric["label"]: metric["value"] for metric in response.context["agenda_metrics"]}
        self.assertEqual(metrics["Citas activas"], "03")
        self.assertEqual(metrics["Tramos con citas"], "03")
        self.assertEqual(metrics["Confirmadas"], "02")
        self.assertEqual(metrics["Canceladas"], "01")

    def test_operational_day_without_weekly_availability_shows_all_base_slots_as_available(self):
        today = timezone.localdate()

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

    def test_cancelled_metric_counts_selected_day_without_reintroducing_cancelled_entries(self):
        today = timezone.localdate()
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
        self.assertEqual(metrics["Canceladas"], "01")
        self.assertEqual(slot_09["entries"], [])
        self.assertEqual(slot_09["available_label"], "Disponible")
        self.assertNotContains(response, self.tertiary_client.name)

    def test_daily_panel_complete_slot_keeps_edit_links_and_hides_create_action(self):
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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
        today = timezone.localdate()
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


class AgendaSettingsViewTests(AuthenticatedAgendaBaseTestCase):
    def test_settings_page_creates_singleton_and_renders_empty_states(self):
        response = self.client.get(reverse("core:agenda_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "core/agenda_settings.html")
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
        self.assertContains(response, 'name="year"')
        self.assertContains(response, reverse("core:manual_closure_create"))
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
