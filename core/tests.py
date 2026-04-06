from datetime import datetime, time, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Appointment, AvailabilityBlock, Client, Service, WeeklyAvailability


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

    def _create_weekly_availability(self, target_day, slot_times):
        for slot_time in slot_times:
            WeeklyAvailability.objects.create(weekday=target_day.weekday(), slot_time=slot_time)

    def _create_block(self, target_day, slot_time, label="Bloqueo puntual"):
        return AvailabilityBlock.objects.create(day=target_day, slot_time=slot_time, label=label)

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
            self.primary_client,
            self.review_service,
            today,
            time(12, 0),
            Appointment.Status.CONFIRMED,
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
        self.assertContains(response, "Bloqueo interno")
        self.assertContains(response, "Disponible")
        self.assertContains(response, "Fuera de disponibilidad")

        metrics = {metric["label"]: metric["value"] for metric in response.context["agenda_metrics"]}
        self.assertEqual(metrics["Citas activas"], "03")
        self.assertEqual(metrics["Tramos con citas"], "04")
        self.assertEqual(metrics["Confirmadas"], "02")
        self.assertEqual(metrics["Canceladas"], "01")

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
            ["2 citas", "1 bloqueo"],
        )

    def test_daily_panel_priority_is_appointment_then_block_then_available_then_unavailable(self):
        today = timezone.localdate()
        self._create_weekly_availability(today, ("09:00", "10:00", "11:00"))
        self._create_block(today, "09:00")
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
