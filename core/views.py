from calendar import Calendar, monthrange
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.views.generic import TemplateView

from .models import AGENDA_SLOT_TIMES, Appointment, AvailabilityBlock, WeeklyAvailability


AGENDA_SLOT_VALUES = tuple(time.fromisoformat(value) for value in AGENDA_SLOT_TIMES)
ACTIVE_CALENDAR_STATUS_KEYS = {Appointment.Status.PENDING, Appointment.Status.CONFIRMED}

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
    return [{"time": slot_time, "entries": [], "blocked_label": ""} for slot_time in AGENDA_SLOT_TIMES]


def _slot_label_for_datetime(target_datetime):
    local_time = timezone.localtime(target_datetime).time().replace(second=0, microsecond=0)
    selected_slot = AGENDA_SLOT_TIMES[0]
    for index, slot_time in enumerate(AGENDA_SLOT_VALUES):
        if local_time < slot_time:
            return AGENDA_SLOT_TIMES[index - 1] if index > 0 else selected_slot
        selected_slot = AGENDA_SLOT_TIMES[index]
    return selected_slot


def _build_entry_from_appointment(appointment):
    local_start = timezone.localtime(appointment.start_at)
    return {
        "name": appointment.client.name,
        "service": appointment.service.name,
        "service_label": f"{local_start:%H:%M} · {appointment.service.name}",
        "status": appointment.get_status_display(),
        "status_key": appointment.status,
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


def _blocked_slots_for_day(target_day):
    return {
        block.slot_time: block.label or "Bloqueo puntual"
        for block in AvailabilityBlock.objects.filter(day=target_day).order_by("slot_time", "id")
    }


def _build_timeline_slots(target_day, appointments):
    slots = _empty_timeline_slots()
    slot_map = {slot["time"]: slot for slot in slots}
    available_slots = _available_slots_for_day(target_day)
    blocked_slots = _blocked_slots_for_day(target_day)

    for appointment in appointments:
        slot_map[_slot_label_for_datetime(appointment.start_at)]["entries"].append(
            _build_entry_from_appointment(appointment)
        )

    for slot in slots:
        if slot["entries"]:
            slot["available_label"] = ""
            slot["unavailable_label"] = ""
            continue

        if slot["time"] in blocked_slots:
            slot["blocked_label"] = blocked_slots[slot["time"]]
            slot["available_label"] = ""
            slot["unavailable_label"] = ""
            continue

        if slot["time"] in available_slots:
            slot["available_label"] = "Disponible"
            slot["unavailable_label"] = ""
            continue

        slot["available_label"] = ""
        slot["unavailable_label"] = "Fuera de disponibilidad"

    return slots


def _build_day_summary(timeline_slots):
    slots_with_appointments = sum(1 for slot in timeline_slots if slot["entries"])
    blocked_slots = sum(1 for slot in timeline_slots if not slot["entries"] and slot["blocked_label"])
    available_slots = sum(1 for slot in timeline_slots if slot.get("available_label"))
    unavailable_slots = sum(1 for slot in timeline_slots if slot.get("unavailable_label"))

    summary_parts = []
    if slots_with_appointments:
        summary_parts.append(
            _format_count_label(slots_with_appointments, "tramo con cita", "tramos con cita")
        )
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
            "meta": "pending + confirmed del dia",
        },
        {
            "label": "Tramos con citas",
            "value": f"{occupied_slots:02d}",
            "meta": "bloques visuales con actividad",
        },
        {
            "label": "Confirmadas",
            "value": f"{confirmed_entries:02d}",
            "meta": "citas con estado confirmado",
        },
        {
            "label": "Canceladas",
            "value": f"{cancelled_entries:02d}",
            "meta": "visibles solo en el panel diario",
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
            {
                "agenda_metrics": _build_agenda_metrics(today_panel["agenda_timeline_slots"]),
                "today_context_label": _format_today_context_label(real_today),
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
            }
        )
        context.update(day_panel)
        return context


class UIValidationView(TemplateView):
    template_name = "core/ui_preview.html"


class CalendarUIValidationView(TemplateView):
    template_name = "core/calendar_ui_preview.html"
