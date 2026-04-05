from calendar import Calendar, monthrange
from copy import deepcopy
from datetime import date
from urllib.parse import urlencode

from django.utils import timezone
from django.views.generic import TemplateView


AGENDA_SLOT_TIMES = ("09:00", "10:00", "11:00", "12:00", "13:00", "16:00", "17:00", "18:00")
ACTIVE_CALENDAR_STATUS_KEYS = {"pending", "confirmed"}

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

AGENDA_ENTRY_SEEDS = [
    {"name": "Marta Leon", "service": "Fisio inicial", "status": "Confirmada", "status_key": "confirmed"},
    {"name": "Carlos Ruiz", "service": "Revision", "status": "Confirmada", "status_key": "confirmed"},
    {"name": "Ana Perez", "service": "Seguimiento", "status": "Pendiente", "status_key": "pending"},
    {"name": "Lucia Gomez", "service": "Evaluacion", "status": "Confirmada", "status_key": "confirmed"},
    {"name": "Sofia Marquez", "service": "Control", "status": "Pendiente", "status_key": "pending"},
    {"name": "Raul Soto", "service": "Primera", "status": "Cancelada", "status_key": "cancelled"},
    {"name": "Nora Vidal", "service": "Llamada", "status": "Cancelada", "status_key": "cancelled"},
    {"name": "Laura Vega", "service": "Revision", "status": "Pendiente", "status_key": "pending"},
]

EXPLICIT_DAY_DETAILS = {
    date(2026, 4, 7): {
        "summary": "3 tramos ocupados, 4 limpios y 1 hueco libre largo.",
        "slots": [
            {
                "time": "09:00",
                "entries": [
                    {"name": "Paula Martin", "service": "Revision", "status": "Confirmada", "status_key": "confirmed"}
                ],
                "blocked_label": "",
            },
            {
                "time": "10:00",
                "entries": [
                    {"name": "Diego Lara", "service": "Control", "status": "Pendiente", "status_key": "pending"}
                ],
                "blocked_label": "",
            },
            {"time": "11:00", "entries": [], "blocked_label": ""},
            {
                "time": "12:00",
                "entries": [
                    {"name": "Marta Leon", "service": "Seguimiento", "status": "Confirmada", "status_key": "confirmed"}
                ],
                "blocked_label": "",
            },
            {"time": "13:00", "entries": [], "blocked_label": ""},
            {"time": "16:00", "entries": [], "blocked_label": ""},
            {
                "time": "17:00",
                "entries": [
                    {"name": "Lucia Gomez", "service": "Evaluacion", "status": "Confirmada", "status_key": "confirmed"}
                ],
                "blocked_label": "",
            },
            {"time": "18:00", "entries": [], "blocked_label": ""},
        ],
    },
    date(2026, 4, 8): {
        "summary": "4 tramos ocupados, 3 limpios y 1 bloqueo parcial.",
        "slots": [
            {
                "time": "09:00",
                "entries": [
                    {"name": "Marta Leon", "service": "Fisio inicial", "status": "Confirmada", "status_key": "confirmed"}
                ],
                "blocked_label": "",
            },
            {
                "time": "10:00",
                "entries": [
                    {"name": "Carlos Ruiz", "service": "Revision", "status": "Confirmada", "status_key": "confirmed"},
                    {"name": "Sofia Marquez", "service": "Control", "status": "Pendiente", "status_key": "pending"},
                ],
                "blocked_label": "",
            },
            {"time": "11:00", "entries": [], "blocked_label": ""},
            {
                "time": "12:00",
                "entries": [
                    {"name": "Ana Perez", "service": "Seguimiento", "status": "Pendiente", "status_key": "pending"},
                    {"name": "Raul Soto", "service": "Primera", "status": "Cancelada", "status_key": "cancelled"},
                    {"name": "Nora Vidal", "service": "Llamada", "status": "Cancelada", "status_key": "cancelled"},
                ],
                "blocked_label": "",
            },
            {"time": "13:00", "entries": [], "blocked_label": "Bloqueo parcial"},
            {"time": "16:00", "entries": [], "blocked_label": ""},
            {
                "time": "17:00",
                "entries": [
                    {"name": "Lucia Gomez", "service": "Evaluacion", "status": "Confirmada", "status_key": "confirmed"}
                ],
                "blocked_label": "",
            },
            {"time": "18:00", "entries": [], "blocked_label": ""},
        ],
    },
    date(2026, 4, 10): {
        "summary": "2 tramos ocupados y resto limpio, sin bloqueos.",
        "slots": [
            {
                "time": "09:00",
                "entries": [
                    {"name": "Alberto Cano", "service": "Primera", "status": "Confirmada", "status_key": "confirmed"}
                ],
                "blocked_label": "",
            },
            {"time": "10:00", "entries": [], "blocked_label": ""},
            {"time": "11:00", "entries": [], "blocked_label": ""},
            {
                "time": "12:00",
                "entries": [
                    {"name": "Laura Vega", "service": "Revision", "status": "Pendiente", "status_key": "pending"}
                ],
                "blocked_label": "",
            },
            {"time": "13:00", "entries": [], "blocked_label": ""},
            {"time": "16:00", "entries": [], "blocked_label": ""},
            {"time": "17:00", "entries": [], "blocked_label": ""},
            {"time": "18:00", "entries": [], "blocked_label": ""},
        ],
    },
    date(2026, 4, 15): {
        "summary": "1 bloqueo parcial y resto de la agenda limpia.",
        "slots": [
            {"time": "09:00", "entries": [], "blocked_label": ""},
            {"time": "10:00", "entries": [], "blocked_label": ""},
            {"time": "11:00", "entries": [], "blocked_label": ""},
            {"time": "12:00", "entries": [], "blocked_label": ""},
            {"time": "13:00", "entries": [], "blocked_label": "Bloqueo parcial"},
            {"time": "16:00", "entries": [], "blocked_label": ""},
            {"time": "17:00", "entries": [], "blocked_label": ""},
            {"time": "18:00", "entries": [], "blocked_label": ""},
        ],
    },
}


def _real_today():
    return timezone.localdate()


def _copy_slots(slots):
    copied_slots = deepcopy(slots)
    for slot in copied_slots:
        slot["entries"] = [_normalize_entry(entry) for entry in slot.get("entries", ())]
    return copied_slots


def _normalize_entry(entry):
    normalized_entry = entry.copy()
    status_key = normalized_entry.get("status_key")
    if status_key == "completed":
        normalized_entry["status_key"] = "confirmed"
        normalized_entry["status"] = "Confirmada"
    elif status_key in {"missed", "no_show"}:
        normalized_entry["status_key"] = "cancelled"
        normalized_entry["status"] = "Cancelada"
    return normalized_entry


def _format_day_title(selected_day):
    return f"{WEEKDAY_NAMES[selected_day.weekday()]} {selected_day.day} de {MONTH_NAMES[selected_day.month]}"


def _format_month_title(year, month):
    return f"{MONTH_NAMES[month].capitalize()} {year}"


def _format_today_context_label(target_day):
    return f"Hoy · {target_day.day} {MONTH_NAMES[target_day.month]} {target_day.year}"


def _day_signature(target_day):
    seed = target_day.toordinal()
    if target_day.weekday() >= 5:
        busy_count = 0 if target_day.day % 2 else 1
    else:
        busy_count = (seed % 4) + 1
        if target_day.day % 7 == 0:
            busy_count = min(busy_count + 1, 5)
    has_blocked = target_day.weekday() < 5 and ((target_day.day + target_day.month) % 6 == 0)
    confirmed_count = max(1, busy_count - 1) if busy_count >= 3 and target_day.day % 2 == 0 else 0
    return {"busy_count": busy_count, "has_blocked": has_blocked, "confirmed_count": confirmed_count}


def _build_markers_for_day(target_day, visible_month):
    if target_day.month != visible_month.month or target_day.year != visible_month.year:
        return []

    day_base = _build_day_base(target_day)
    return _build_markers_from_slots(day_base["slots"])


def _build_markers_from_slots(timeline_slots):
    active_entries = sum(
        1
        for slot in timeline_slots
        for entry in slot.get("entries", ())
        if entry.get("status_key") in ACTIVE_CALENDAR_STATUS_KEYS
    )
    confirmed_entries = sum(
        1
        for slot in timeline_slots
        for entry in slot.get("entries", ())
        if entry.get("status_key") == "confirmed"
    )
    has_blocked = any(slot.get("blocked_label") for slot in timeline_slots)

    markers = []
    if active_entries:
        label = "1 cita" if active_entries == 1 else f"{active_entries} citas"
        markers.append({"label": label, "kind": "busy"})
    if confirmed_entries:
        markers.append({"label": f"{confirmed_entries} confirmadas", "kind": "neutral"})
    elif has_blocked:
        markers.append({"label": "bloqueo parcial", "kind": "blocked"})
    return markers[:2]


def _entry_distribution(busy_count):
    distribution_map = {
        0: [],
        1: [1],
        2: [1, 1],
        3: [1, 1, 1],
        4: [1, 2, 1],
        5: [1, 2, 2],
    }
    return distribution_map.get(min(busy_count, 5), [1, 2, 2])


def _entry_for_position(target_day, offset):
    seed = AGENDA_ENTRY_SEEDS[(target_day.day + target_day.month + offset) % len(AGENDA_ENTRY_SEEDS)]
    return _normalize_entry(seed)


def _build_dynamic_slots(target_day, busy_count, has_blocked):
    slots = [{"time": time, "entries": [], "blocked_label": ""} for time in AGENDA_SLOT_TIMES]
    slot_map = {slot["time"]: slot for slot in slots}
    occupied_times = ("09:00", "10:00", "12:00")
    entry_offset = 0

    for index, count in enumerate(_entry_distribution(busy_count)):
        time = occupied_times[index]
        slot_map[time]["entries"] = [_entry_for_position(target_day, entry_offset + position) for position in range(count)]
        entry_offset += count

    if has_blocked:
        slot_map["13:00"]["blocked_label"] = "Bloqueo parcial"

    return slots


def _build_dynamic_summary(busy_count, has_blocked):
    occupied_slot_count = len(_entry_distribution(busy_count))
    if occupied_slot_count and has_blocked:
        return f"{occupied_slot_count} tramos ocupados, {8 - occupied_slot_count - 1} limpios y 1 bloqueo parcial."
    if occupied_slot_count:
        return f"{occupied_slot_count} tramos ocupados y resto limpio."
    if has_blocked:
        return "1 bloqueo parcial y resto limpio."
    return "Agenda ligera, sin citas previstas ni bloqueos."


def _build_day_base(selected_day):
    explicit_detail = EXPLICIT_DAY_DETAILS.get(selected_day)
    if explicit_detail:
        return {
            "summary": explicit_detail["summary"],
            "slots": _copy_slots(explicit_detail["slots"]),
        }

    signature = _day_signature(selected_day)
    return {
        "summary": _build_dynamic_summary(signature["busy_count"], signature["has_blocked"]),
        "slots": _build_dynamic_slots(selected_day, signature["busy_count"], signature["has_blocked"]),
    }


def _build_day_panel(selected_day):
    day_base = _build_day_base(selected_day)
    return {
        "selected_day_title": _format_day_title(selected_day),
        "selected_day_summary": day_base["summary"],
        "agenda_timeline_slots": day_base["slots"],
    }


def _build_agenda_metrics(timeline_slots):
    total_entries = sum(len(slot.get("entries", ())) for slot in timeline_slots)
    free_slots = sum(1 for slot in timeline_slots if not slot.get("entries") and not slot.get("blocked_label"))
    confirmed_entries = sum(
        1
        for slot in timeline_slots
        for entry in slot.get("entries", ())
        if entry.get("status_key") == "confirmed"
    )
    blocked_slots = sum(1 for slot in timeline_slots if slot.get("blocked_label"))

    return [
        {
            "label": "Citas de hoy",
            "value": f"{total_entries:02d}",
            "meta": "entradas previstas en la agenda",
        },
        {
            "label": "Huecos libres",
            "value": f"{free_slots:02d}",
            "meta": "tramos sin cita ni bloqueo",
        },
        {
            "label": "Confirmadas",
            "value": f"{confirmed_entries:02d}",
            "meta": "citas con estado confirmado",
        },
        {
            "label": "Bloqueos de hoy",
            "value": f"{blocked_slots:02d}",
            "meta": "tramos bloqueados en la jornada",
        },
    ]


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


def _build_agenda_weeks(visible_year, visible_month, selected_day, real_today):
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
                    "today": (week_day == real_today and not is_outside),
                    "selected": week_day == selected_day,
                    "markers": _build_markers_for_day(week_day, visible_month_date),
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
        context.update(
            {
                "agenda_metrics": _build_agenda_metrics(today_panel["agenda_timeline_slots"]),
                "today_context_label": _format_today_context_label(real_today),
                "weekday_labels": WEEKDAY_SHORT_LABELS,
                "agenda_weeks": _build_agenda_weeks(visible_year, visible_month, selected_day, real_today),
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
