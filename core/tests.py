from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Appointment, AvailabilityBlock, Client, Service, WeeklyAvailability


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

    def _create_weekly_availability(self, target_day, slot_times, capacity=3):
        for slot_time in slot_times:
            WeeklyAvailability.objects.create(
                weekday=target_day.weekday(),
                slot_time=slot_time,
                capacity=capacity,
            )

    def _create_block(self, target_day, slot_time, label="Bloqueo puntual"):
        return AvailabilityBlock.objects.create(day=target_day, slot_time=slot_time, label=label)

    def login_app_user(self):
        self.client.force_login(self.app_user)


class AuthenticatedAgendaBaseTestCase(AgendaBaseTestCase):
    def setUp(self):
        super().setUp()
        self.login_app_user()


class AppointmentSlotValidationTests(AgendaBaseTestCase):
    def test_valid_appointment_in_available_slot_can_be_created(self):
        today = timezone.localdate()
        self._create_weekly_availability(today, ("11:00",), capacity=2)

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
        self._create_weekly_availability(today, ("11:00",), capacity=2)

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
        self._create_weekly_availability(today, ("09:00", "10:00"), capacity=1)

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
        self._create_weekly_availability(today, ("11:00",), capacity=2)
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

    def test_appointment_outside_availability_is_rejected(self):
        today = timezone.localdate()
        self._create_weekly_availability(today, ("10:00",), capacity=2)

        appointment = self._build_appointment(
            self.primary_client,
            self.review_service,
            today,
            time(11, 0),
            Appointment.Status.PENDING,
        )

        with self.assertRaises(ValidationError) as raised:
            appointment.save()

        self.assertIn("fuera de la disponibilidad", str(raised.exception))

    def test_multiple_appointments_are_allowed_until_slot_capacity(self):
        today = timezone.localdate()
        self._create_weekly_availability(today, ("11:00",), capacity=2)

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
        self._create_weekly_availability(today, ("11:00",), capacity=2)
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

        with self.assertRaises(ValidationError) as raised:
            third.save()

        self.assertIn("capacidad maxima", str(raised.exception))

    def test_slot_with_capacity_three_accepts_three_active_appointments_and_rejects_a_fourth(self):
        today = timezone.localdate()
        self._create_weekly_availability(today, ("11:00",), capacity=3)

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
        self._create_weekly_availability(today, ("09:00",), capacity=1)
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
        self._create_weekly_availability(today, ("11:00",), capacity=2)

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
        self._create_weekly_availability(today, ("09:00", "10:00"), capacity=1)
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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"), capacity=2)
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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"), capacity=2)

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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"), capacity=2)

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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"), capacity=2)

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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"), capacity=2)

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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"), capacity=2)

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
        self._create_weekly_availability(today, ("11:00",), capacity=2)

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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"), capacity=1)
        self._create_block(today, "10:00", label="Bloqueo interno")
        self._create_appointment(
            self.primary_client,
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
        self._create_weekly_availability(today, ("09:00",), capacity=1)
        self._create_appointment(
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
        self._create_weekly_availability(today, ("11:00",), capacity=2)
        self._create_block(today, "11:00", label="Bloqueo interno")

        response = self.client.post(reverse("core:appointment_create"), self._appointment_form_data())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "bloqueado")
        self.assertEqual(Appointment.objects.count(), 0)

    def test_create_view_rejects_slot_outside_availability(self):
        today = timezone.localdate()
        self._create_weekly_availability(today, ("10:00",), capacity=2)

        response = self.client.post(reverse("core:appointment_create"), self._appointment_form_data())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "fuera de la disponibilidad")
        self.assertEqual(Appointment.objects.count(), 0)

    def test_create_view_rejects_complete_slot(self):
        today = timezone.localdate()
        self._create_weekly_availability(today, ("11:00",), capacity=2)
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

        response = self.client.post(
            reverse("core:appointment_create"),
            self._appointment_form_data(client=self.tertiary_client.pk),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "capacidad maxima")
        self.assertEqual(Appointment.objects.count(), 2)

    def test_create_view_accepts_third_appointment_when_capacity_is_three_and_rejects_fourth(self):
        today = timezone.localdate()
        self._create_weekly_availability(today, ("11:00",), capacity=3)
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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"), capacity=3)
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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"), capacity=2)
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
        self._create_weekly_availability(today, ("09:00",), capacity=1)
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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"), capacity=2)
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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"), capacity=1)
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

    def test_update_view_shows_cancellation_notice_when_appointment_is_cancelled(self):
        today = timezone.localdate()
        self._create_weekly_availability(today, ("09:00",), capacity=1)
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
        self._create_weekly_availability(today, ("09:00",), capacity=1)
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
        self._create_weekly_availability(today, ("09:00",), capacity=1)
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

    def test_update_view_requires_explicit_delete_confirmation_and_then_removes_appointment(self):
        today = timezone.localdate()
        self._create_weekly_availability(today, ("09:00",), capacity=1)
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

        self._create_weekly_availability(today, ("09:00", "12:00"), capacity=2)
        self._create_weekly_availability(previous_day, ("10:00",), capacity=2)
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
        self._create_weekly_availability(today, ("09:00",), capacity=1)
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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00", "12:00", "16:00", "17:00"))
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

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Claudia Real")
        self.assertContains(response, "10:00")
        self.assertContains(response, "Bloqueo interno")
        self.assertContains(response, "Disponible")
        self.assertContains(response, "Fuera de disponibilidad")
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
        self.assertEqual(metrics["Canceladas"], "00")

    def test_daily_panel_complete_slot_keeps_edit_links_and_hides_create_action(self):
        today = timezone.localdate()
        agenda_query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        expected_next = f"{reverse('core:app_entrypoint')}?{agenda_query}"
        expected_create_url = (
            f"{reverse('core:appointment_create')}"
            f"?{urlencode({'year': today.year, 'month': today.month, 'day': today.day, 'slot_time': '09:00', 'next': expected_next})}"
        )
        expected_create_href = expected_create_url.replace("&", "&amp;")
        self._create_weekly_availability(today, ("09:00",), capacity=3)
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
        selected_day = today + timedelta(days=1)
        self._create_weekly_availability(selected_day, ("09:00", "10:00", "11:00"))
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

    def test_daily_panel_empty_slot_exposes_create_action_with_contextual_url(self):
        today = timezone.localdate()
        agenda_query = urlencode({"year": today.year, "month": today.month, "day": today.day})
        expected_next = f"{reverse('core:app_entrypoint')}?{agenda_query}"
        expected_create_url = (
            f"{reverse('core:appointment_create')}"
            f"?{urlencode({'year': today.year, 'month': today.month, 'day': today.day, 'slot_time': '10:00', 'next': expected_next})}"
        )
        expected_create_href = expected_create_url.replace("&", "&amp;")
        self._create_weekly_availability(today, ("10:00",), capacity=2)

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
        self._create_weekly_availability(today, ("09:00",), capacity=3)
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
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"))
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
        self.assertEqual(slot_12["unavailable_label"], "Fuera de disponibilidad")

    def test_daily_panel_waits_for_third_active_entry_before_marking_capacity_three_slot_as_complete(self):
        today = timezone.localdate()
        self._create_weekly_availability(today, ("09:00",), capacity=3)
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
        self._create_weekly_availability(today, ("09:00", "10:00"), capacity=2)
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
