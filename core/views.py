from calendar import Calendar, monthrange
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.generic import FormView, TemplateView

from .forms import AppointmentForm
from .models import (
    AGENDA_SLOT_TIMES,
    Appointment,
    AvailabilityBlock,
    WeeklyAvailability,
    agenda_assigned_slot_time,
)


ACTIVE_CALENDAR_STATUS_KEYS = set(Appointment.active_statuses())

WEEKDAY_SHORT_LABELS = ("Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom")
WEEKDAY_NAMES = ("Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo")
MONTH_NAMES = {
    1: "enero",
    2: "febrero",
    3: "marzo",
    4: "abril",
    5: "mayo",
    6: "junio",
    7: "julio",
    8: "agosto",
    9: "septiembre",
    10: "octubre",
    11: "noviembre",
    12: "diciembre",
}


def _real_today():
    return timezone.localdate()


def _current_timezone():
    return timezone.get_current_timezone()


def _aware_day_start(target_day):
    return timezone.make_aware(datetime.combine(target_day, time.min), _current_timezone())


def _day_bounds(target_day):
    start_at = _aware_day_start(target_day)
    return start_at, start_at + timedelta(days=1)


def _query_string(year, month, day):
    return urlencode({"year": year, "month": month, "day": day})


def _adjacent_month(year, month, delta):
    target_month = month + delta
    target_year = year
    if target_month == 0:
        target_month = 12
        target_year -= 1
    elif target_month == 13:
        target_month = 1
        target_year += 1
    return target_year, target_month


def _navigation_query(year, month, selected_day_number, delta):
    target_year, target_month = _adjacent_month(year, month, delta)
    max_day = monthrange(target_year, target_month)[1]
    target_day = selected_day_number if selected_day_number <= max_day else 1
    return _query_string(target_year, target_month, target_day)


def _build_calendar_context(
    *,
    visible_year,
    visible_month,
    selected_day,
    real_today,
    month_summary,
    calendar_base_url,
    calendar_hx_enabled,
    calendar_hx_target_id="",
):
    return {
        "weekday_labels": WEEKDAY_SHORT_LABELS,
        "agenda_weeks": _build_agenda_weeks(
            visible_year,
            visible_month,
            selected_day,
            real_today,
            month_summary,
        ),
        "visible_month_title": _format_month_title(visible_year, visible_month),
        "previous_month_query": _navigation_query(visible_year, visible_month, selected_day.day, -1),
        "next_month_query": _navigation_query(visible_year, visible_month, selected_day.day, 1),
        "selected_day_iso": selected_day.isoformat(),
        "selected_day_query": _query_string(selected_day.year, selected_day.month, selected_day.day),
        "today_query": _query_string(real_today.year, real_today.month, real_today.day),
        "calendar_base_url": calendar_base_url,
        "calendar_hx_enabled": calendar_hx_enabled,
        "calendar_hx_target_id": calendar_hx_target_id,
    }


def _agenda_url_for_day(target_day):
    return f"{reverse('core:app_entrypoint')}?{_query_string(target_day.year, target_day.month, target_day.day)}"


def _format_count_label(count, singular, plural):
    return f"{count} {singular if count == 1 else plural}"


def _join_summary_parts(parts):
    if not parts:
        return "Agenda ligera, sin disponibilidad configurada."
    if len(parts) == 1:
        return f"{parts[0]}."
    return f"{', '.join(parts[:-1])} y {parts[-1]}."


def _format_day_title(selected_day):
    return f"{WEEKDAY_NAMES[selected_day.weekday()]} {selected_day.day} de {MONTH_NAMES[selected_day.month]}"


def _format_month_title(year, month):
    return f"{MONTH_NAMES[month].capitalize()} {year}"


def _format_today_context_label(target_day):
    return f"Hoy · {target_day.day} {MONTH_NAMES[target_day.month]} {target_day.year}"


def _empty_timeline_slots():
    return [
        {
            "time": slot_time,
            "entries": [],
            "blocked_label": "",
            "complete_label": "",
            "available_label": "",
            "unavailable_label": "",
        }
        for slot_time in AGENDA_SLOT_TIMES
    ]


def _build_entry_from_appointment(appointment, selected_day):
    slot_time = agenda_assigned_slot_time(appointment.start_at)
    edit_query = _query_string(selected_day.year, selected_day.month, selected_day.day)
    return {
        "id": appointment.pk,
        "name": appointment.client.name,
        "service": appointment.service.name,
        "service_label": appointment.service.name,
        "status": appointment.get_status_display(),
        "status_key": appointment.status,
        "edit_url": f"{reverse('core:appointment_update', args=[appointment.pk])}?{edit_query}",
        "edit_label": f"Editar cita de {appointment.client.name}",
    }


def _appointments_for_day(target_day):
    start_at, end_at = _day_bounds(target_day)
    return list(
        Appointment.objects.select_related("client", "service")
        .filter(start_at__gte=start_at, start_at__lt=end_at)
        .order_by("start_at", "id")
    )


def _available_slots_for_day(target_day):
    return set(
        WeeklyAvailability.objects.filter(weekday=target_day.weekday()).values_list("slot_time", flat=True)
    )


def _available_slot_capacities_for_day(target_day):
    return dict(
        WeeklyAvailability.objects.filter(weekday=target_day.weekday()).values_list("slot_time", "capacity")
    )


def _blocked_slots_for_day(target_day):
    return {
        block.slot_time: block.label or "Bloqueo puntual"
        for block in AvailabilityBlock.objects.filter(day=target_day).order_by("slot_time", "id")
    }


def _build_timeline_slots(target_day, appointments):
    slots = _empty_timeline_slots()
    slot_map = {slot["time"]: slot for slot in slots}
    available_slot_capacities = _available_slot_capacities_for_day(target_day)
    available_slots = _available_slots_for_day(target_day)
    blocked_slots = _blocked_slots_for_day(target_day)

    for appointment in appointments:
        slot_map[agenda_assigned_slot_time(appointment.start_at)]["entries"].append(
            _build_entry_from_appointment(appointment, target_day)
        )

    for slot in slots:
        if slot["entries"]:
            active_entries = sum(
                1 for entry in slot["entries"] if entry["status_key"] in ACTIVE_CALENDAR_STATUS_KEYS
            )
            slot_capacity = available_slot_capacities.get(slot["time"])
            if slot_capacity and active_entries >= slot_capacity and slot["time"] not in blocked_slots:
                slot["complete_label"] = "Completo"
            continue

        if slot["time"] in blocked_slots:
            slot["blocked_label"] = blocked_slots[slot["time"]]
            continue

        if slot["time"] in available_slots:
            slot["available_label"] = "Disponible"
            continue

        slot["unavailable_label"] = "Fuera de disponibilidad"

    return slots


def _build_day_summary(timeline_slots):
    slots_with_appointments = sum(1 for slot in timeline_slots if slot["entries"])
    complete_slots = sum(1 for slot in timeline_slots if slot.get("complete_label"))
    blocked_slots = sum(1 for slot in timeline_slots if not slot["entries"] and slot["blocked_label"])
    available_slots = sum(1 for slot in timeline_slots if slot.get("available_label"))
    unavailable_slots = sum(1 for slot in timeline_slots if slot.get("unavailable_label"))

    summary_parts = []
    if slots_with_appointments:
        summary_parts.append(
            _format_count_label(slots_with_appointments, "tramo con cita", "tramos con cita")
        )
    if complete_slots:
        summary_parts.append(_format_count_label(complete_slots, "tramo completo", "tramos completos"))
    if blocked_slots:
        summary_parts.append(_format_count_label(blocked_slots, "bloqueo", "bloqueos"))
    if available_slots:
        summary_parts.append(
            _format_count_label(available_slots, "tramo disponible", "tramos disponibles")
        )
    if unavailable_slots:
        summary_parts.append(
            _format_count_label(
                unavailable_slots,
                "tramo fuera de disponibilidad",
                "tramos fuera de disponibilidad",
            )
        )
    return _join_summary_parts(summary_parts)


def _build_day_panel(selected_day):
    appointments = _appointments_for_day(selected_day)
    timeline_slots = _build_timeline_slots(selected_day, appointments)
    return {
        "selected_day_title": _format_day_title(selected_day),
        "selected_day_summary": _build_day_summary(timeline_slots),
        "agenda_timeline_slots": timeline_slots,
    }


def _build_agenda_metrics(timeline_slots):
    active_entries = sum(
        1
        for slot in timeline_slots
        for entry in slot["entries"]
        if entry["status_key"] in ACTIVE_CALENDAR_STATUS_KEYS
    )
    occupied_slots = sum(1 for slot in timeline_slots if slot["entries"])
    confirmed_entries = sum(
        1
        for slot in timeline_slots
        for entry in slot["entries"]
        if entry["status_key"] == Appointment.Status.CONFIRMED
    )
    cancelled_entries = sum(
        1
        for slot in timeline_slots
        for entry in slot["entries"]
        if entry["status_key"] == Appointment.Status.CANCELLED
    )

    return [
        {
            "label": "Citas activas",
            "value": f"{active_entries:02d}",
            "meta": "pendientes y confirmadas del día",
        },
        {
            "label": "Tramos con citas",
            "value": f"{occupied_slots:02d}",
            "meta": "tramos ocupados del día",
        },
        {
            "label": "Confirmadas",
            "value": f"{confirmed_entries:02d}",
            "meta": "citas ya confirmadas",
        },
        {
            "label": "Canceladas",
            "value": f"{cancelled_entries:02d}",
            "meta": "siguen visibles en el panel",
        },
    ]


def _month_summary(visible_year, visible_month):
    month_start = date(visible_year, visible_month, 1)
    next_year, next_month = _adjacent_month(visible_year, visible_month, 1)
    month_end = date(next_year, next_month, 1)

    appointment_rows = (
        Appointment.objects.filter(
            start_at__gte=_aware_day_start(month_start),
            start_at__lt=_aware_day_start(month_end),
        )
        .annotate(local_day=TruncDate("start_at", tzinfo=_current_timezone()))
        .values("local_day")
        .annotate(
            active_count=Count("id", filter=Q(status__in=ACTIVE_CALENDAR_STATUS_KEYS)),
            confirmed_count=Count("id", filter=Q(status=Appointment.Status.CONFIRMED)),
        )
        .order_by("local_day")
    )

    block_rows = (
        AvailabilityBlock.objects.filter(day__gte=month_start, day__lt=month_end)
        .values("day")
        .annotate(block_count=Count("id"))
        .order_by("day")
    )

    summary = {
        row["local_day"]: {
            "active_count": row["active_count"],
            "confirmed_count": row["confirmed_count"],
            "block_count": 0,
        }
        for row in appointment_rows
    }

    for row in block_rows:
        day_summary = summary.setdefault(
            row["day"],
            {"active_count": 0, "confirmed_count": 0, "block_count": 0},
        )
        day_summary["block_count"] = row["block_count"]

    return summary


def _build_markers_for_day(target_day, visible_month_date, month_summary):
    if target_day.month != visible_month_date.month or target_day.year != visible_month_date.year:
        return []

    day_summary = month_summary.get(target_day, {})
    active_entries = day_summary.get("active_count", 0)
    confirmed_entries = day_summary.get("confirmed_count", 0)
    blocked_slots = day_summary.get("block_count", 0)

    markers = []
    if active_entries:
        markers.append(
            {
                "label": _format_count_label(active_entries, "cita", "citas"),
                "kind": "busy",
            }
        )
    if blocked_slots:
        markers.append(
            {
                "label": _format_count_label(blocked_slots, "bloqueo", "bloqueos"),
                "kind": "blocked",
            }
        )
    elif confirmed_entries:
        markers.append(
            {
                "label": _format_count_label(confirmed_entries, "confirmada", "confirmadas"),
                "kind": "neutral",
            }
        )
    return markers[:2]


def _build_agenda_weeks(visible_year, visible_month, selected_day, real_today, month_summary):
    month_calendar = Calendar(firstweekday=0).monthdatescalendar(visible_year, visible_month)
    visible_month_date = date(visible_year, visible_month, 1)
    weeks = []
    for week in month_calendar:
        week_days = []
        for week_day in week:
            is_outside = week_day.month != visible_month or week_day.year != visible_year
            week_days.append(
                {
                    "number": week_day.day,
                    "outside": is_outside,
                    "today": week_day == real_today and not is_outside,
                    "selected": week_day == selected_day,
                    "markers": _build_markers_for_day(week_day, visible_month_date, month_summary),
                    "year": week_day.year,
                    "month": week_day.month,
                    "iso_date": week_day.isoformat(),
                    "querystring": _query_string(week_day.year, week_day.month, week_day.day),
                }
            )
        weeks.append(week_days)
    return weeks


def _resolve_calendar_state(request):
    real_today = _real_today()
    raw_year = request.GET.get("year")
    raw_month = request.GET.get("month")
    raw_day = request.GET.get("day")

    if raw_year is None and raw_month is None and raw_day is None:
        return real_today.year, real_today.month, real_today, real_today

    try:
        year = int(raw_year)
        month = int(raw_month)
        day = int(raw_day)
        selected_day = date(year, month, day)
    except (TypeError, ValueError):
        return real_today.year, real_today.month, real_today, real_today

    return year, month, selected_day, real_today


class AppEntryPointView(TemplateView):
    template_name = "core/app_entrypoint.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        visible_year, visible_month, selected_day, real_today = _resolve_calendar_state(self.request)
        day_panel = _build_day_panel(selected_day)
        today_panel = _build_day_panel(real_today)
        month_summary = _month_summary(visible_year, visible_month)
        context.update(
            _build_calendar_context(
                visible_year=visible_year,
                visible_month=visible_month,
                selected_day=selected_day,
                real_today=real_today,
                month_summary=month_summary,
                calendar_base_url=reverse("core:app_entrypoint"),
                calendar_hx_enabled=True,
                calendar_hx_target_id="agenda-interactive-region",
            )
        )
        context.update(
            {
                "agenda_metrics": _build_agenda_metrics(today_panel["agenda_timeline_slots"]),
                "today_context_label": _format_today_context_label(real_today),
            }
        )
        context.update(day_panel)
        return context


class AppointmentFormViewBase(FormView):
    template_name = "core/appointment_form.html"
    form_class = AppointmentForm

    def get_appointment(self):
        return None

    def get_selected_day(self):
        if self.request.method == "POST":
            raw_day = self.request.POST.get("day")
            if raw_day:
                try:
                    return date.fromisoformat(raw_day)
                except ValueError:
                    pass

        raw_year = self.request.GET.get("year")
        raw_month = self.request.GET.get("month")
        raw_day = self.request.GET.get("day")
        if raw_year is not None and raw_month is not None and raw_day is not None:
            _, _, selected_day, _ = _resolve_calendar_state(self.request)
            return selected_day

        appointment = self.get_appointment()
        if appointment is not None:
            return appointment.slot_day
        return _real_today()

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.get_appointment()
        kwargs["initial_day"] = self.get_selected_day()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        appointment = self.get_appointment()
        selected_day = self.get_selected_day()
        is_edit = appointment is not None
        context.update(
            {
                "page_title": "Editar cita" if is_edit else "Nueva cita",
                "page_description": (
                    "Ajusta cliente, servicio, fecha y tramo. La validacion sigue la disponibilidad,"
                    " los bloqueos y la capacidad real del tramo."
                ),
                "submit_label": "Guardar cambios" if is_edit else "Guardar cita",
                "back_url": _agenda_url_for_day(selected_day),
                "is_edit": is_edit,
            }
        )
        return context

    def form_valid(self, form):
        appointment = form.save()
        return HttpResponseRedirect(_agenda_url_for_day(appointment.slot_day))


class AppointmentCreateView(AppointmentFormViewBase):
    template_name = "core/appointment_create_screen.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        selected_day = self.get_selected_day()
        visible_year = selected_day.year
        visible_month = selected_day.month
        real_today = _real_today()
        month_summary = _month_summary(visible_year, visible_month)
        day_panel = _build_day_panel(selected_day)
        context.update(
            _build_calendar_context(
                visible_year=visible_year,
                visible_month=visible_month,
                selected_day=selected_day,
                real_today=real_today,
                month_summary=month_summary,
                calendar_base_url=reverse("core:appointment_create"),
                calendar_hx_enabled=False,
            )
        )
        context.update(day_panel)
        context.update(
            {
                "create_screen_context_label": "Nueva cita · contexto de agenda",
                "slot_selection_message": "Selecciona un tramo disponible para preparar la cita.",
            }
        )
        return context


class AppointmentUpdateView(AppointmentFormViewBase):
    def get_appointment(self):
        if not hasattr(self, "_appointment"):
            self._appointment = get_object_or_404(Appointment, pk=self.kwargs["pk"])
        return self._appointment


class UIValidationView(TemplateView):
    template_name = "core/ui_preview.html"


class CalendarUIValidationView(TemplateView):
    template_name = "core/calendar_ui_preview.html"
