from datetime import datetime, time, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Appointment, Client, Service


class AppEntryPointViewTests(TestCase):
    def setUp(self):
        self.review_service = Service.objects.create(name="Revision", duration_minutes=45, color="#3158D7")
        self.control_service = Service.objects.create(name="Control", duration_minutes=30, color="#2E7A58")
        self.primary_client = Client.objects.create(name="Claudia Real")
        self.secondary_client = Client.objects.create(name="Mario Real")
        self.tertiary_client = Client.objects.create(name="Nora Real")

    def _create_appointment(self, client, service, target_day, start_time, status):
        start_at = timezone.make_aware(
            datetime.combine(target_day, start_time),
            timezone.get_current_timezone(),
        )
        return Appointment.objects.create(
            client=client,
            service=service,
            start_at=start_at,
            end_at=start_at + timedelta(minutes=service.duration_minutes),
            status=status,
        )

    def _day_context(self, response, target_day):
        for week in response.context["agenda_weeks"]:
            for week_day in week:
                if week_day["iso_date"] == target_day.isoformat():
                    return week_day
        self.fail(f"Day {target_day.isoformat()} not found in agenda_weeks context.")

    def test_agenda_metrics_and_panel_use_database_appointments(self):
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
            time(10, 30),
            Appointment.Status.PENDING,
        )
        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            today,
            time(17, 0),
            Appointment.Status.CANCELLED,
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": today.year, "month": today.month, "day": today.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Claudia Real")
        self.assertContains(response, "10:30")
        self.assertContains(response, "Cancelada")

        metrics = {metric["label"]: metric["value"] for metric in response.context["agenda_metrics"]}
        self.assertEqual(metrics["Citas activas"], "02")
        self.assertEqual(metrics["Tramos con citas"], "03")
        self.assertEqual(metrics["Confirmadas"], "01")
        self.assertEqual(metrics["Canceladas"], "01")

    def test_month_markers_count_only_pending_and_confirmed(self):
        today = timezone.localdate()
        selected_day = today + timedelta(days=1)
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
        self._create_appointment(
            self.tertiary_client,
            self.review_service,
            selected_day,
            time(12, 0),
            Appointment.Status.CANCELLED,
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        day_context = self._day_context(response, selected_day)
        self.assertEqual(
            [marker["label"] for marker in day_context["markers"]],
            ["2 citas", "1 confirmada"],
        )

    def test_selected_day_panel_changes_with_querystring_and_keeps_cancelled_visible(self):
        today = timezone.localdate()
        selected_day = today + timedelta(days=2)
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
            selected_day,
            time(12, 30),
            Appointment.Status.CANCELLED,
        )

        response = self.client.get(
            reverse("core:app_entrypoint"),
            {"year": selected_day.year, "month": selected_day.month, "day": selected_day.day},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Mario Real")
        self.assertContains(response, "12:30")
        self.assertNotContains(response, "Claudia Real")
        self.assertContains(response, "cancelada visible")

        metrics = {metric["label"]: metric["value"] for metric in response.context["agenda_metrics"]}
        self.assertEqual(metrics["Citas activas"], "01")
        self.assertEqual(metrics["Canceladas"], "00")
