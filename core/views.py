from calendar import Calendar, monthrange
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode, urlsplit

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login, logout as auth_logout
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Prefetch, Q
from django.db.models.functions import TruncDate
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import FormView, TemplateView

from .day_availability import DayAvailabilityResolver
from .forms import (
    AgendaSettingsForm,
    AppointmentForm,
    BusinessSettingsForm,
    ClientForm,
    ManualClosureForm,
    OfficialHolidaySyncForm,
    ServiceForm,
)
from .management.commands.sync_official_holidays import BoeSyncError, import_boe_national_holidays
from .models import (
    AGENDA_SLOT_TIMES,
    AgendaSettings,
    Appointment,
    AvailabilityBlock,
    BusinessSettings,
    Client,
    ManualClosure,
    OfficialHoliday,
    Service,
    agenda_assigned_slot_time,
    agenda_slot_operational_state_map,
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
    calendar_interactive=True,
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
        "calendar_interactive": calendar_interactive,
        "calendar_hx_target_id": calendar_hx_target_id,
    }


def _agenda_url_for_day(target_day):
    return f"{reverse('core:app_entrypoint')}?{_query_string(target_day.year, target_day.month, target_day.day)}"


def _format_count_label(count, singular, plural):
    return f"{count} {singular if count == 1 else plural}"


def _join_summary_parts(parts):
    if not parts:
        return "Agenda ligera, sin actividad todavia."
    if len(parts) == 1:
        return f"{parts[0]}."
    return f"{', '.join(parts[:-1])} y {parts[-1]}."


def _format_day_title(selected_day):
    return f"{WEEKDAY_NAMES[selected_day.weekday()]} {selected_day.day} de {MONTH_NAMES[selected_day.month]}"


def _format_month_title(year, month):
    return f"{MONTH_NAMES[month].capitalize()} {year}"


def _format_today_context_label(target_day):
    return f"Hoy · {target_day.day} {MONTH_NAMES[target_day.month]} {target_day.year}"


def _format_compact_day(target_day):
    return f"{target_day.day} {MONTH_NAMES[target_day.month]} {target_day.year}"


def _format_next_appointment_label(appointment):
    if appointment is None:
        return "Sin proxima cita"

    slot_day = appointment.slot_day
    slot_time = appointment.slot_time
    if slot_day is None:
        return "Sin proxima cita"
    if not slot_time:
        return _format_compact_day(slot_day)
    return f"{_format_compact_day(slot_day)} · {slot_time}"


def _parse_iso_day(raw_day):
    if not raw_day:
        return None
    try:
        return date.fromisoformat(raw_day)
    except ValueError:
        return None


def _safe_next_url(request):
    raw_next = request.POST.get("next") or request.GET.get("next", "")
    if raw_next and url_has_allowed_host_and_scheme(
        raw_next,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return raw_next
    return ""


def _safe_appointment_next_url(request):
    raw_next = request.POST.get("appointment_next") or request.GET.get("appointment_next", "")
    if raw_next and url_has_allowed_host_and_scheme(
        raw_next,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return raw_next
    return ""


def _url_with_next(base_url, next_url):
    if not next_url:
        return base_url
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'next': next_url})}"


def _client_detail_navigation_url(client, next_url=""):
    detail_url = reverse("core:client_detail", args=[client.pk])
    if next_url and next_url.startswith(detail_url):
        return next_url
    return _url_with_next(detail_url, next_url)


def _url_path(raw_url):
    if not raw_url:
        return ""
    return urlsplit(raw_url).path


def _appointment_create_url(target_day=None, slot_time="", next_url="", client_id=None):
    query_params = {}

    if target_day is not None:
        query_params.update(
            {
                "year": target_day.year,
                "month": target_day.month,
                "day": target_day.day,
            }
        )

    if slot_time in AGENDA_SLOT_TIMES:
        query_params["slot_time"] = slot_time

    if next_url:
        query_params["next"] = next_url

    if client_id not in (None, ""):
        query_params["client"] = client_id

    base_url = reverse("core:appointment_create")
    if not query_params:
        return base_url
    return f"{base_url}?{urlencode(query_params)}"


def _appointment_repeat_url(source_appointment, *, next_url=""):
    base_url = _appointment_create_url(next_url=next_url)
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'source_appointment': source_appointment.pk})}"


def _client_create_url(target_day=None, slot_time="", appointment_next_url=""):
    query_params = {}

    if target_day is not None:
        query_params.update(
            {
                "year": target_day.year,
                "month": target_day.month,
                "day": target_day.day,
            }
        )

    if slot_time in AGENDA_SLOT_TIMES:
        query_params["slot_time"] = slot_time

    if appointment_next_url:
        query_params["appointment_next"] = appointment_next_url

    base_url = reverse("core:client_create")
    if not query_params:
        return base_url
    return f"{base_url}?{urlencode(query_params)}"


def _appointment_create_url_for_slot(target_day, slot_time, next_url=""):
    return _appointment_create_url(
        target_day=target_day,
        slot_time=slot_time,
        next_url=next_url,
    )


def _agenda_settings_url():
    return reverse("core:agenda_settings")


def _settings_index_url():
    return reverse("core:settings_index")


def _service_settings_url():
    return reverse("core:service_settings")


def _business_settings_url():
    return reverse("core:business_settings")


def _archived_client_list_url():
    return reverse("core:archived_client_list")


def _settings_breadcrumbs(*items):
    return [
        {"label": "Ajustes", "url": _settings_index_url()},
        *items,
    ]


def _client_breadcrumbs(*items):
    return [
        {"label": "Clientes", "url": reverse("core:client_list")},
        *items,
    ]


def _empty_timeline_slots():
    return [
        {
            "time": slot_time,
            "entries": [],
            "active_entries_count": 0,
            "capacity": None,
            "busy_label": "",
            "blocked_label": "",
            "complete_label": "",
            "available_label": "",
            "unavailable_label": "",
            "can_book": False,
            "create_url": "",
            "create_label": "",
            "block_action_label": "",
        }
        for slot_time in AGENDA_SLOT_TIMES
    ]


def _build_entry_from_appointment(appointment, selected_day):
    slot_time = agenda_assigned_slot_time(appointment.start_at)
    edit_query = _query_string(selected_day.year, selected_day.month, selected_day.day)
    return {
        "id": appointment.pk,
        "name": appointment.client.name,
        "service": appointment.services_label,
        "service_label": appointment.services_label,
        "status": appointment.get_status_display(),
        "status_key": appointment.status,
        "edit_url": f"{reverse('core:appointment_update', args=[appointment.pk])}?{edit_query}",
        "edit_label": f"Editar cita de {appointment.client.name}",
    }


def _appointments_for_day(target_day):
    start_at, end_at = _day_bounds(target_day)
    return list(
        Appointment.objects.select_related("client")
        .prefetch_related("services")
        .filter(
            start_at__gte=start_at,
            start_at__lt=end_at,
            status__in=Appointment.active_statuses(),
        )
        .order_by("start_at", "id")
    )


def _format_busy_slot_label(active_count, capacity):
    if capacity:
        occupied_label = "ocupada" if active_count == 1 else "ocupadas"
        return f"{active_count}/{capacity} {occupied_label}"
    active_label = "cita activa" if active_count == 1 else "citas activas"
    return f"{active_count} {active_label}"


def _day_status_kind(day_availability):
    if day_availability.status == DayAvailabilityResolver.MANUAL_CLOSURE:
        return "manual-closure"
    if day_availability.status in {
        DayAvailabilityResolver.NON_WORKING_SATURDAY,
        DayAvailabilityResolver.NON_WORKING_SUNDAY,
    }:
        return "non-working"
    return "working"


def _is_closed_day(day_availability):
    return day_availability is not None and not day_availability.is_working_day


def _day_availability_resolver_for_range(start_day, end_day, *, agenda_settings=None):
    settings = agenda_settings or AgendaSettings.get_solo()
    manual_closures = ManualClosure.objects.filter(
        start_date__lte=end_day,
        end_date__gte=start_day,
    ).order_by("start_date", "end_date", "id")
    return DayAvailabilityResolver(
        agenda_settings=settings,
        manual_closures=manual_closures,
    )


def _month_day_availability_map(visible_year, visible_month, *, agenda_settings=None):
    month_last_day_number = monthrange(visible_year, visible_month)[1]
    month_start = date(visible_year, visible_month, 1)
    month_last_day = date(visible_year, visible_month, month_last_day_number)
    resolver = _day_availability_resolver_for_range(
        month_start,
        month_last_day,
        agenda_settings=agenda_settings,
    )
    return {
        date(visible_year, visible_month, day_number): resolver.resolve(
            date(visible_year, visible_month, day_number)
        )
        for day_number in range(1, month_last_day_number + 1)
    }


def _can_create_block_for_slot_snapshot(slot_snapshot):
    if not slot_snapshot:
        return False
    if slot_snapshot.get("blocked_label"):
        return False
    return slot_snapshot.get("active_count", 0) == 0


def _build_timeline_slots(target_day, appointments, return_url="", day_availability=None):
    slots = _empty_timeline_slots()
    slot_map = {slot["time"]: slot for slot in slots}
    slot_state_map = agenda_slot_operational_state_map(target_day)
    resolved_day = day_availability or DayAvailabilityResolver.resolve_for_global_agenda(target_day)
    day_is_closed = _is_closed_day(resolved_day)

    for appointment in appointments:
        slot_map[agenda_assigned_slot_time(appointment.start_at)]["entries"].append(
            _build_entry_from_appointment(appointment, target_day)
        )

    for slot in slots:
        slot_snapshot = slot_state_map[slot["time"]]
        slot["active_entries_count"] = slot_snapshot["active_count"]
        slot["capacity"] = slot_snapshot["capacity"]
        slot["can_book"] = slot_snapshot["can_book"] and not day_is_closed

        if slot["can_book"]:
            slot["create_url"] = _appointment_create_url_for_slot(
                target_day,
                slot["time"],
                next_url=return_url or _agenda_url_for_day(target_day),
            )
            slot["create_label"] = "Nueva cita"

        if not day_is_closed:
            if slot_snapshot["blocked_label"]:
                slot["block_action_label"] = "Quitar bloqueo"
            elif _can_create_block_for_slot_snapshot(slot_snapshot):
                slot["block_action_label"] = "Bloquear"

        if slot["entries"]:
            if slot_snapshot["is_complete"]:
                slot["complete_label"] = "Completo"
            elif slot_snapshot["active_count"]:
                slot["busy_label"] = _format_busy_slot_label(
                    slot_snapshot["active_count"],
                    slot_snapshot["capacity"],
                )
            else:
                slot["busy_label"] = "Sin ocupacion activa"
            continue

        if day_is_closed:
            slot["unavailable_label"] = resolved_day.label
            continue

        if slot_snapshot["blocked_label"]:
            slot["blocked_label"] = slot_snapshot["blocked_label"]
            continue

        slot["available_label"] = "Disponible"

    return slots


def _build_day_summary(timeline_slots, day_availability=None):
    if _is_closed_day(day_availability):
        return day_availability.label

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


def _build_day_panel(selected_day, return_url="", agenda_settings=None):
    day_availability = DayAvailabilityResolver.resolve_for_global_agenda(
        selected_day,
        agenda_settings=agenda_settings,
    )
    appointments = _appointments_for_day(selected_day)
    timeline_slots = _build_timeline_slots(
        selected_day,
        appointments,
        return_url=return_url,
        day_availability=day_availability,
    )
    return {
        "selected_day_title": _format_day_title(selected_day),
        "selected_day_summary": _build_day_summary(timeline_slots, day_availability=day_availability),
        "selected_day_is_working_day": day_availability.is_working_day,
        "selected_day_status_label": day_availability.label,
        "selected_day_status_kind": _day_status_kind(day_availability),
        "selected_day_status_notes": (
            day_availability.manual_closure.notes if day_availability.manual_closure else ""
        ),
        "selected_day_secondary_notice": (
            f"Es {day_availability.official_holiday.name}, pero la agenda esta abierta."
            if day_availability.is_working_day and day_availability.official_holiday is not None
            else ""
        ),
        "agenda_timeline_slots": timeline_slots,
    }


def _build_agenda_metrics(timeline_slots):
    active_entries = 0
    pending_entries = 0
    confirmed_entries = 0
    free_slots = 0

    for slot in timeline_slots:
        if slot["can_book"]:
            free_slots += 1

        for entry in slot["entries"]:
            if entry["status_key"] in ACTIVE_CALENDAR_STATUS_KEYS:
                active_entries += 1
            if entry["status_key"] == Appointment.Status.PENDING:
                pending_entries += 1
            elif entry["status_key"] == Appointment.Status.CONFIRMED:
                confirmed_entries += 1

    return [
        {
            "label": "Citas activas",
            "value": f"{active_entries:02d}",
            "meta": "ocupan agenda ese dia",
        },
        {
            "label": "Pendientes",
            "value": f"{pending_entries:02d}",
            "meta": "citas por confirmar",
        },
        {
            "label": "Confirmadas",
            "value": f"{confirmed_entries:02d}",
            "meta": "citas ya confirmadas",
        },
        {
            "label": "Huecos libres",
            "value": f"{free_slots:02d}",
            "meta": "tramos donde aun puedes citar",
        },
    ]


def _month_summary(visible_year, visible_month, *, agenda_settings=None):
    month_start = date(visible_year, visible_month, 1)
    next_year, next_month = _adjacent_month(visible_year, visible_month, 1)
    month_end = date(next_year, next_month, 1)
    day_availability_map = _month_day_availability_map(
        visible_year,
        visible_month,
        agenda_settings=agenda_settings,
    )

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
            "day_availability": day_availability_map.get(row["local_day"]),
        }
        for row in appointment_rows
    }

    for row in block_rows:
        day_summary = summary.setdefault(
            row["day"],
            {"active_count": 0, "confirmed_count": 0, "block_count": 0, "day_availability": None},
        )
        day_summary["block_count"] = row["block_count"]

    for target_day, day_availability in day_availability_map.items():
        day_summary = summary.setdefault(
            target_day,
            {"active_count": 0, "confirmed_count": 0, "block_count": 0, "day_availability": None},
        )
        day_summary["day_availability"] = day_availability

    return summary


def _build_markers_for_day(target_day, visible_month_date, month_summary):
    if target_day.month != visible_month_date.month or target_day.year != visible_month_date.year:
        return []

    day_summary = month_summary.get(target_day, {})
    active_entries = day_summary.get("active_count", 0)
    confirmed_entries = day_summary.get("confirmed_count", 0)
    blocked_slots = day_summary.get("block_count", 0)
    day_availability = day_summary.get("day_availability")
    day_is_closed = _is_closed_day(day_availability)

    markers = []
    if active_entries:
        markers.append(
            {
                "label": _format_count_label(active_entries, "cita", "citas"),
                "kind": "busy",
            }
        )
    if blocked_slots and not day_is_closed:
        markers.append(
            {
                "label": _format_count_label(blocked_slots, "bloqueo", "bloqueos"),
                "kind": "blocked",
            }
        )
    elif confirmed_entries and not day_is_closed:
        markers.append(
            {
                "label": _format_count_label(confirmed_entries, "confirmada", "confirmadas"),
                "kind": "neutral",
            }
        )
    return markers[:2]


def _build_month_primary_state(day_availability):
    if day_availability is None or day_availability.is_working_day:
        return None

    if day_availability.status == DayAvailabilityResolver.NON_WORKING_SATURDAY:
        return {
            "kind": "non-working",
            "lines": ["Sábado", "no laborable"],
        }

    if day_availability.status == DayAvailabilityResolver.NON_WORKING_SUNDAY:
        return {
            "kind": "non-working",
            "lines": ["Domingo", "no laborable"],
        }

    if day_availability.official_holiday is not None:
        return {
            "kind": "official-holiday",
            "lines": [day_availability.official_holiday.name],
        }

    if day_availability.manual_closure is not None:
        return {
            "kind": "manual-closure",
            "lines": [day_availability.label],
        }

    return {
        "kind": "non-working",
        "lines": [day_availability.label],
    }


def _month_day_status_kind(day_availability):
    primary_state = _build_month_primary_state(day_availability)
    if primary_state is None:
        return ""
    return primary_state["kind"]


def _build_agenda_weeks(visible_year, visible_month, selected_day, real_today, month_summary):
    month_calendar = Calendar(firstweekday=0).monthdatescalendar(visible_year, visible_month)
    visible_month_date = date(visible_year, visible_month, 1)
    weeks = []
    for week in month_calendar:
        week_days = []
        for week_day in week:
            is_outside = week_day.month != visible_month or week_day.year != visible_year
            day_availability = month_summary.get(week_day, {}).get("day_availability")
            primary_state = _build_month_primary_state(day_availability)
            day_status_kind = _month_day_status_kind(day_availability)
            secondary_holiday_marker = ""
            secondary_holiday_marker_title = ""
            if primary_state is None and day_availability is not None and day_availability.official_holiday is not None:
                secondary_holiday_marker = day_availability.official_holiday.name
                secondary_holiday_marker_title = (
                    f"{day_availability.official_holiday.name}. La agenda esta abierta."
                )
            week_days.append(
                {
                    "number": week_day.day,
                    "outside": is_outside,
                    "today": week_day == real_today and not is_outside,
                    "selected": week_day == selected_day,
                    "primary_state": primary_state,
                    "markers": _build_markers_for_day(week_day, visible_month_date, month_summary),
                    "secondary_holiday_marker": secondary_holiday_marker,
                    "secondary_holiday_marker_title": secondary_holiday_marker_title,
                    "status_kind": day_status_kind,
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


def app_login_view(request):
    next_url = _safe_next_url(request)
    redirect_target = next_url or reverse("core:app_entrypoint")

    if request.user.is_authenticated:
        return redirect(redirect_target)

    if request.method == "POST":
        form = AuthenticationForm(request=request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.get_user())
            return redirect(redirect_target)
    else:
        form = AuthenticationForm(request=request)

    return render(
        request,
        "core/login.html",
        {
            "form": form,
            "next_url": next_url,
            "demo_access_username": settings.DEMO_ACCESS_USERNAME,
            "demo_access_password": settings.DEMO_ACCESS_PASSWORD,
            "demo_reset_notice": settings.DEMO_RESET_NOTICE,
        },
    )


@require_POST
def app_logout_view(request):
    auth_logout(request)
    return redirect("core:login")


class AppLoginRequiredMixin(LoginRequiredMixin):
    login_url = reverse_lazy("core:login")

    def get_business_settings(self):
        return BusinessSettings.get_solo()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        business_settings = self.get_business_settings()
        business_name = "Tu negocio"
        is_business_configured = False

        if business_settings and business_settings.is_configured:
            business_name = business_settings.display_name
            is_business_configured = True

        context.update(
            {
                "app_business_name": business_name,
                "app_business_is_configured": is_business_configured,
            }
        )
        return context


class AppEntryPointView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/app_entrypoint.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        visible_year, visible_month, selected_day, real_today = _resolve_calendar_state(self.request)
        agenda_settings = AgendaSettings.get_solo()
        day_panel = _build_day_panel(
            selected_day,
            return_url=self.request.get_full_path(),
            agenda_settings=agenda_settings,
        )
        month_summary = _month_summary(
            visible_year,
            visible_month,
            agenda_settings=agenda_settings,
        )
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
                "agenda_metrics": _build_agenda_metrics(day_panel["agenda_timeline_slots"]),
                "today_context_label": _format_today_context_label(real_today),
            }
        )
        context.update(day_panel)
        return context


class SettingsIndexView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/settings_index.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        agenda_settings_url = _agenda_settings_url()
        context.update(
            {
                "settings_groups": [
                    {
                        "title": "Datos del negocio",
                        "description": "Configura la identidad basica del negocio que usa esta agenda.",
                        "items": [
                            {
                                "label": "Datos del negocio",
                                "description": "Nombre, contacto y datos fiscales de la instancia actual.",
                                "url": _business_settings_url(),
                            },
                        ],
                    },
                    {
                        "title": "Agenda y disponibilidad",
                        "description": "Gestiona horarios, cierres y dias no operativos.",
                        "items": [
                            {
                                "label": "Ajustes de agenda",
                                "description": "Reglas base de fines de semana y festivos.",
                                "url": agenda_settings_url,
                            },
                            {
                                "label": "Cierres manuales",
                                "description": "Vacaciones, puentes y cierres completos del negocio.",
                                "url": f"{agenda_settings_url}#cierres-manuales",
                            },
                            {
                                "label": "Festivos oficiales",
                                "description": "Festivos sincronizados desde BOE en solo lectura.",
                                "url": f"{agenda_settings_url}#festivos-oficiales",
                            },
                        ],
                    },
                    {
                        "title": "Servicios",
                        "description": "Define los servicios que puedes reservar en tu agenda.",
                        "items": [
                            {
                                "label": "Servicios",
                                "description": "Gestiona el catalogo operativo de servicios.",
                                "url": _service_settings_url(),
                            },
                        ],
                    },
                ],
            }
        )
        return context


class BusinessSettingsView(AppLoginRequiredMixin, FormView):
    template_name = "core/business_settings.html"
    form_class = BusinessSettingsForm

    def get_settings(self):
        return BusinessSettings.get_solo() or BusinessSettings(pk=1)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.get_settings()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "page_title": "Datos del negocio",
                "page_description": (
                    "Configura una unica ficha basica del negocio para esta instancia de la app."
                ),
                "settings_breadcrumbs": _settings_breadcrumbs(
                    {"label": "Datos del negocio", "url": ""},
                ),
            }
        )
        return context

    def form_valid(self, form):
        form.save()
        messages.success(self.request, "Datos del negocio guardados.")
        return HttpResponseRedirect(_business_settings_url())


class AgendaSettingsView(AppLoginRequiredMixin, FormView):
    template_name = "core/agenda_settings.html"
    form_class = AgendaSettingsForm
    SETTINGS_ACTION = "update_settings"
    SYNC_OFFICIAL_HOLIDAYS_ACTION = "sync_official_holidays"
    CLEAR_SYNC_FAILURE_ACTION = "clear_sync_failure"

    def get_settings(self):
        return AgendaSettings.get_solo()

    def get_settings_form(self, *, data=None):
        kwargs = {"instance": self.get_settings()}
        if data is not None:
            kwargs["data"] = data
        return AgendaSettingsForm(**kwargs)

    def get_sync_form(self, *, data=None):
        kwargs = {}
        if data is not None:
            kwargs["data"] = data
        else:
            kwargs["initial"] = {"year": _real_today().year}
        return OfficialHolidaySyncForm(**kwargs)

    def get_manual_closures(self):
        return ManualClosure.objects.all()

    def get_official_holidays(self):
        return OfficialHoliday.objects.filter(source=OfficialHoliday.Source.BOE_NATIONAL_SYNC)

    def get_last_boe_sync_trace(self, agenda_settings):
        if not agenda_settings.has_boe_sync_trace:
            return None

        return {
            "synced_at": agenda_settings.last_boe_sync_at,
            "year": agenda_settings.last_boe_sync_year,
            "resolution_identifier": agenda_settings.last_boe_sync_resolution_identifier,
            "resolution_title": agenda_settings.last_boe_sync_resolution_title,
            "resolution_url": agenda_settings.last_boe_sync_resolution_url,
            "created_count": agenda_settings.last_boe_sync_created_count,
            "skipped_existing_count": agenda_settings.last_boe_sync_skipped_existing_count,
            "error_count": agenda_settings.last_boe_sync_error_count,
        }

    def get_last_boe_sync_failure_trace(self, agenda_settings):
        if not agenda_settings.has_boe_sync_failure_trace:
            return None

        return {
            "failed_at": agenda_settings.last_boe_sync_failure_at,
            "year": agenda_settings.last_boe_sync_failure_year,
            "message": agenda_settings.last_boe_sync_failure_message,
        }

    def get(self, request, *args, **kwargs):
        return self.render_to_response(
            self.get_context_data(
                form=self.get_settings_form(),
                sync_form=self.get_sync_form(),
            )
        )

    def post(self, request, *args, **kwargs):
        action = request.POST.get("agenda_action", self.SETTINGS_ACTION)

        if action == self.SYNC_OFFICIAL_HOLIDAYS_ACTION:
            return self._handle_sync_post()
        if action == self.CLEAR_SYNC_FAILURE_ACTION:
            return self._handle_clear_sync_failure_post()
        return self._handle_settings_post()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.setdefault("form", self.get_settings_form())
        context.setdefault("sync_form", self.get_sync_form())
        agenda_settings = context["form"].instance
        context.update(
            {
                "page_title": "Ajustes de agenda",
                "page_description": (
                    "La agenda ya parte de una parrilla fija de 8 tramos por dia operativo."
                    " Aqui solo ajustas fines de semana, cierres completos y festivos oficiales."
                ),
                "back_url": reverse("core:app_entrypoint"),
                "settings_breadcrumbs": _settings_breadcrumbs(
                    {"label": "Agenda y disponibilidad", "url": ""},
                ),
                "manual_closures": self.get_manual_closures(),
                "manual_closure_create_url": reverse("core:manual_closure_create"),
                "official_holidays": self.get_official_holidays(),
                "last_boe_sync_trace": self.get_last_boe_sync_trace(agenda_settings),
                "last_boe_sync_failure_trace": self.get_last_boe_sync_failure_trace(agenda_settings),
            }
        )
        return context

    def _store_sync_trace(self, result):
        agenda_settings = self.get_settings()
        agenda_settings.last_boe_sync_at = timezone.now()
        agenda_settings.last_boe_sync_year = result.target_year
        agenda_settings.last_boe_sync_resolution_identifier = result.resolution.identifier
        agenda_settings.last_boe_sync_resolution_title = result.resolution.title
        agenda_settings.last_boe_sync_resolution_url = result.resolution.url_html
        agenda_settings.last_boe_sync_created_count = result.created_count
        agenda_settings.last_boe_sync_skipped_existing_count = result.skipped_existing_count
        agenda_settings.last_boe_sync_error_count = result.error_count
        agenda_settings.save(
            update_fields=[
                "last_boe_sync_at",
                "last_boe_sync_year",
                "last_boe_sync_resolution_identifier",
                "last_boe_sync_resolution_title",
                "last_boe_sync_resolution_url",
                "last_boe_sync_created_count",
                "last_boe_sync_skipped_existing_count",
                "last_boe_sync_error_count",
            ]
        )

    def _store_sync_failure_trace(self, target_year, error_message):
        agenda_settings = self.get_settings()
        agenda_settings.last_boe_sync_failure_at = timezone.now()
        agenda_settings.last_boe_sync_failure_year = target_year
        agenda_settings.last_boe_sync_failure_message = error_message or "Error desconocido durante el sync BOE."
        agenda_settings.save(
            update_fields=[
                "last_boe_sync_failure_at",
                "last_boe_sync_failure_year",
                "last_boe_sync_failure_message",
            ]
        )

    def _clear_sync_failure_trace(self):
        agenda_settings = self.get_settings()
        agenda_settings.last_boe_sync_failure_at = None
        agenda_settings.last_boe_sync_failure_year = None
        agenda_settings.last_boe_sync_failure_message = ""
        agenda_settings.save(
            update_fields=[
                "last_boe_sync_failure_at",
                "last_boe_sync_failure_year",
                "last_boe_sync_failure_message",
            ]
        )

    def _handle_settings_post(self):
        form = self.get_settings_form(data=self.request.POST)
        if form.is_valid():
            form.save()
            messages.success(self.request, "Configuracion guardada.")
            return HttpResponseRedirect(_agenda_settings_url())

        return self.render_to_response(
            self.get_context_data(
                form=form,
                sync_form=self.get_sync_form(),
            )
        )

    def _handle_sync_post(self):
        sync_form = self.get_sync_form(data=self.request.POST)
        if not sync_form.is_valid():
            return self.render_to_response(
                self.get_context_data(
                    form=self.get_settings_form(),
                    sync_form=sync_form,
                )
            )

        target_year = sync_form.cleaned_data["year"]
        try:
            result = import_boe_national_holidays(target_year)
        except (requests.RequestException, BoeSyncError) as error:
            error_message = str(error)
            self._store_sync_failure_trace(target_year, error_message)
            messages.error(self.request, f"No se pudo completar la importacion BOE {target_year}: {error_message}")
        else:
            self._store_sync_trace(result)
            messages.success(
                self.request,
                (
                    f"Importacion BOE {target_year} completada. "
                    f"Creados: {result.created_count}. "
                    f"Ignorados existentes: {result.skipped_existing_count}. "
                    f"Reconciliados: {result.reconciled_count}. "
                    f"Errores: {result.error_count}."
                ),
            )
        return HttpResponseRedirect(_agenda_settings_url())

    def _handle_clear_sync_failure_post(self):
        if self.get_settings().has_boe_sync_failure_trace:
            self._clear_sync_failure_trace()
            messages.success(self.request, "Traza del ultimo fallo BOE limpiada.")
        return HttpResponseRedirect(_agenda_settings_url())


class AvailabilityBlockToggleView(AppLoginRequiredMixin, View):
    FIXED_BLOCK_LABEL = "Bloqueo puntual"

    def post(self, request, *args, **kwargs):
        target_day = _parse_iso_day(request.POST.get("day", ""))
        slot_time = request.POST.get("slot_time", "")

        if target_day is None:
            return HttpResponseRedirect(_agenda_url_for_day(_real_today()))

        redirect_url = _agenda_url_for_day(target_day)

        if slot_time not in AGENDA_SLOT_TIMES:
            return HttpResponseRedirect(redirect_url)

        day_availability = DayAvailabilityResolver.resolve_for_global_agenda(target_day)
        if not day_availability.is_working_day:
            return HttpResponseRedirect(redirect_url)

        existing_block = AvailabilityBlock.objects.filter(day=target_day, slot_time=slot_time).first()
        if existing_block is not None:
            existing_block.delete()
            return HttpResponseRedirect(redirect_url)

        slot_snapshot = agenda_slot_operational_state_map(target_day).get(slot_time, {})
        if not _can_create_block_for_slot_snapshot(slot_snapshot):
            return HttpResponseRedirect(redirect_url)

        AvailabilityBlock.objects.create(
            day=target_day,
            slot_time=slot_time,
            label=self.FIXED_BLOCK_LABEL,
        )
        return HttpResponseRedirect(redirect_url)


class AppointmentFormViewBase(AppLoginRequiredMixin, FormView):
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
        kwargs["initial_slot_time"] = self.get_initial_slot_time()
        kwargs["initial_client_id"] = self.get_initial_client_id()
        kwargs["initial_service_ids"] = self.get_initial_service_ids()
        return kwargs

    def get_initial_slot_time(self):
        raw_slot_time = self.request.GET.get("slot_time", "")
        if raw_slot_time in AGENDA_SLOT_TIMES:
            return raw_slot_time
        return None

    def get_initial_client_id(self):
        raw_client_id = self.request.GET.get("client", "").strip()
        if raw_client_id:
            return raw_client_id

        source_appointment = self.get_source_appointment()
        if source_appointment is None:
            return None
        return source_appointment.client_id

    def get_source_appointment(self):
        if hasattr(self, "_source_appointment"):
            return self._source_appointment

        raw_source_appointment_id = self.request.GET.get("source_appointment", "").strip()
        if not raw_source_appointment_id:
            self._source_appointment = None
            return self._source_appointment

        try:
            normalized_source_appointment_id = int(raw_source_appointment_id)
        except (TypeError, ValueError):
            self._source_appointment = None
            return self._source_appointment

        self._source_appointment = (
            Appointment.objects.select_related("client")
            .prefetch_related("services")
            .filter(pk=normalized_source_appointment_id)
            .first()
        )
        return self._source_appointment

    def get_initial_service_ids(self):
        source_appointment = self.get_source_appointment()
        if source_appointment is None:
            return []
        return list(source_appointment.services.values_list("pk", flat=True))

    def get_back_label(self, back_url):
        if _url_path(back_url) == reverse("core:client_list"):
            return "Volver a clientes"

        appointment = self.get_appointment()
        client_id = appointment.client_id if appointment is not None else self.get_initial_client_id()
        if client_id and _url_path(back_url) == reverse("core:client_detail", args=[client_id]):
            return "Volver a ficha"

        return "Volver a la agenda"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        appointment = self.get_appointment()
        selected_day = self.get_selected_day()
        is_edit = appointment is not None
        back_url = _safe_next_url(self.request) or _agenda_url_for_day(selected_day)
        client_detail_url = ""
        if appointment is not None and appointment.client_id:
            client_detail_url = _url_with_next(
                reverse("core:client_detail", args=[appointment.client_id]),
                self.request.get_full_path(),
            )
        context.update(
            {
                "page_title": "Editar cita" if is_edit else "Nueva cita",
                "page_description": (
                    "Ajusta cliente, servicio, fecha y tramo. La validacion sigue las reglas"
                    " operativas del dia, los bloqueos y la capacidad real del tramo."
                ),
                "submit_label": "Guardar cambios" if is_edit else "Guardar cita",
                "back_url": back_url,
                "back_label": self.get_back_label(back_url),
                "client_detail_url": client_detail_url,
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
        agenda_settings = AgendaSettings.get_solo()
        month_summary = _month_summary(
            visible_year,
            visible_month,
            agenda_settings=agenda_settings,
        )
        day_panel = _build_day_panel(selected_day, agenda_settings=agenda_settings)
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
        current_slot_time = context["form"]["slot_time"].value() or self.get_initial_slot_time() or ""
        context["client_create_url"] = _client_create_url(
            target_day=selected_day,
            slot_time=current_slot_time,
            appointment_next_url=context["back_url"],
        )
        return context


class AppointmentUpdateView(AppointmentFormViewBase):
    SHOW_DELETE_CONFIRMATION_INTENT = "show_delete_confirmation"
    DISMISS_DELETE_CONFIRMATION_INTENT = "dismiss_delete_confirmation"
    CONFIRM_DELETE_INTENT = "confirm_delete"

    def get_appointment(self):
        if not hasattr(self, "_appointment"):
            self._appointment = get_object_or_404(
                Appointment.objects.exclude(status=Appointment.Status.CANCELLED),
                pk=self.kwargs["pk"],
            )
        return self._appointment

    def get_selected_day(self):
        if self.request.method == "POST":
            raw_day = self.request.POST.get("day")
            if raw_day:
                try:
                    return date.fromisoformat(raw_day)
                except ValueError:
                    pass
        return self.get_appointment().slot_day

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        selected_day = self.get_selected_day()
        visible_year = selected_day.year
        visible_month = selected_day.month
        real_today = _real_today()
        month_summary = _month_summary(
            visible_year,
            visible_month,
            agenda_settings=AgendaSettings.get_solo(),
        )
        context.update(
            _build_calendar_context(
                visible_year=visible_year,
                visible_month=visible_month,
                selected_day=selected_day,
                real_today=real_today,
                month_summary=month_summary,
                calendar_base_url=reverse("core:appointment_update", args=[self.get_appointment().pk]),
                calendar_hx_enabled=False,
                calendar_interactive=False,
            )
        )
        context.update(
            {
                "selected_day_title": _format_day_title(selected_day),
                "delete_mode": kwargs.pop("delete_mode", self._delete_mode_requested()),
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        intent = request.POST.get("edit_intent", "")

        if intent == self.CONFIRM_DELETE_INTENT and self._delete_mode_requested():
            appointment = self.get_appointment()
            redirect_day = appointment.slot_day
            appointment.delete()
            return HttpResponseRedirect(_agenda_url_for_day(redirect_day))

        if intent in {
            self.SHOW_DELETE_CONFIRMATION_INTENT,
            self.DISMISS_DELETE_CONFIRMATION_INTENT,
        }:
            form = self.get_form()
            delete_mode = intent == self.SHOW_DELETE_CONFIRMATION_INTENT
            return self.render_to_response(self.get_context_data(form=form, delete_mode=delete_mode))

        return super().post(request, *args, **kwargs)

    def _delete_mode_requested(self):
        return self.request.POST.get("delete_mode") == "true"


class ManualClosureFormViewBase(AppLoginRequiredMixin, FormView):
    template_name = "core/manual_closure_form.html"
    form_class = ManualClosureForm

    def get_manual_closure(self):
        return None

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.get_manual_closure()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        manual_closure = self.get_manual_closure()
        is_edit = manual_closure is not None
        context.update(
            {
                "page_title": "Editar cierre manual" if is_edit else "Nuevo cierre manual",
                "page_description": (
                    "Define un cierre de dia completo o un rango completo para la agenda global."
                ),
                "submit_label": "Guardar cambios" if is_edit else "Guardar cierre",
                "back_url": _agenda_settings_url(),
                "is_edit": is_edit,
            }
        )
        return context

    def form_valid(self, form):
        form.save()
        return HttpResponseRedirect(_agenda_settings_url())


class ManualClosureCreateView(ManualClosureFormViewBase):
    pass


class ManualClosureUpdateView(ManualClosureFormViewBase):
    def get_manual_closure(self):
        if not hasattr(self, "_manual_closure"):
            self._manual_closure = get_object_or_404(ManualClosure, pk=self.kwargs["pk"])
        return self._manual_closure


class ManualClosureDeleteView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/manual_closure_confirm_delete.html"

    def get_manual_closure(self):
        if not hasattr(self, "_manual_closure"):
            self._manual_closure = get_object_or_404(ManualClosure, pk=self.kwargs["pk"])
        return self._manual_closure

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        manual_closure = self.get_manual_closure()
        context.update(
            {
                "page_title": "Eliminar cierre manual",
                "page_description": "Confirma si quieres eliminar este cierre completo de la agenda.",
                "manual_closure": manual_closure,
                "back_url": _agenda_settings_url(),
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        self.get_manual_closure().delete()
        return HttpResponseRedirect(_agenda_settings_url())


class ClientDetailView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/client_detail.html"

    def get_client(self):
        if not hasattr(self, "_client"):
            self._client = get_object_or_404(Client, pk=self.kwargs["pk"])
        return self._client

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        client = self.get_client()
        current_url = self.request.get_full_path()
        next_url = _safe_next_url(self.request)
        fallback_url = _archived_client_list_url() if client.is_archived else reverse("core:app_entrypoint")
        history_items = []

        appointments = client.appointments.prefetch_related("services").order_by("-start_at", "-id")
        for appointment in appointments:
            slot_day = appointment.slot_day
            can_edit = (not client.is_archived) and appointment.status != Appointment.Status.CANCELLED
            edit_url = ""
            if can_edit:
                edit_url = reverse("core:appointment_update", args=[appointment.pk])
                if slot_day is not None:
                    edit_url = f"{edit_url}?{_query_string(slot_day.year, slot_day.month, slot_day.day)}"
            repeat_url = ""
            if not client.is_archived:
                repeat_url = _appointment_repeat_url(
                    appointment,
                    next_url=current_url,
                )
            history_items.append(
                {
                    "date_label": _format_compact_day(slot_day) if slot_day is not None else "",
                    "slot_time": appointment.slot_time,
                    "service_label": appointment.services_label,
                    "status_label": appointment.get_status_display(),
                    "status_key": appointment.status,
                    "can_edit": can_edit,
                    "edit_url": edit_url,
                    "repeat_url": repeat_url,
                }
            )

        context.update(
            {
                "client_obj": client,
                "client_history": history_items,
                "client_breadcrumbs": (
                    _client_breadcrumbs(
                        {"label": "Clientes archivados", "url": _archived_client_list_url()},
                        {"label": client.name, "url": ""},
                    )
                    if client.is_archived
                    else _client_breadcrumbs(
                        {"label": client.name, "url": ""}
                    )
                ),
                "appointment_create_url": (
                    _appointment_create_url(
                        next_url=current_url,
                        client_id=client.pk,
                    )
                    if not client.is_archived
                    else ""
                ),
                "client_update_url": (
                    _url_with_next(
                        reverse("core:client_update", args=[client.pk]),
                        current_url,
                    )
                    if not client.is_archived
                    else ""
                ),
                "client_archive_url": (
                    _url_with_next(
                        reverse("core:client_archive", args=[client.pk]),
                        current_url,
                    )
                    if not client.is_archived
                    else ""
                ),
                "archived_client_list_url": _archived_client_list_url(),
                "back_url": next_url or fallback_url,
            }
        )
        return context


class ClientListView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/client_list.html"

    def get_search_query(self):
        return " ".join(self.request.GET.get("q", "").split())

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        search_query = self.get_search_query()
        current_url = reverse("core:client_list")
        if search_query:
            current_url = f"{current_url}?{urlencode({'q': search_query})}"
        future_appointments = Prefetch(
            "appointments",
            queryset=Appointment.objects.filter(
                start_at__gte=timezone.now(),
                status__in=Appointment.active_statuses(),
            ).order_by("start_at", "id"),
            to_attr="future_active_appointments",
        )
        active_clients = Client.objects.active()
        has_active_clients = active_clients.exists()
        if search_query:
            active_clients = active_clients.filter(
                Q(name__icontains=search_query)
                | Q(phone__icontains=search_query)
                | Q(email__icontains=search_query)
            )
        clients = list(
            active_clients.order_by("name", "id").prefetch_related(future_appointments)
        )

        for client in clients:
            next_appointment = (
                client.future_active_appointments[0]
                if client.future_active_appointments
                else None
            )
            client.next_appointment_label = _format_next_appointment_label(next_appointment)
            client.detail_url = _url_with_next(
                reverse("core:client_detail", args=[client.pk]),
                current_url,
            )
            client.appointment_create_url = _appointment_create_url(
                next_url=current_url,
                client_id=client.pk,
            )

        context.update(
            {
                "clients": clients,
                "client_breadcrumbs": _client_breadcrumbs(),
                "client_create_url": _url_with_next(
                    reverse("core:client_create"),
                    current_url,
                ),
                "archived_client_list_url": _archived_client_list_url(),
                "client_list_url": reverse("core:client_list"),
                "has_active_clients": has_active_clients,
                "search_query": search_query,
            }
        )
        return context


class ClientCreateView(AppLoginRequiredMixin, FormView):
    template_name = "core/client_form.html"
    form_class = ClientForm

    def _raw_context_value(self, key):
        return self.request.POST.get(key) or self.request.GET.get(key, "")

    def get_selected_day(self):
        raw_year = self._raw_context_value("year")
        raw_month = self._raw_context_value("month")
        raw_day = self._raw_context_value("day")

        try:
            return date(int(raw_year), int(raw_month), int(raw_day))
        except (TypeError, ValueError):
            return None

    def get_slot_time(self):
        raw_slot_time = self._raw_context_value("slot_time")
        if raw_slot_time in AGENDA_SLOT_TIMES:
            return raw_slot_time
        return ""

    def get_appointment_next_url(self):
        return _safe_appointment_next_url(self.request)

    def get_next_url(self):
        return _safe_next_url(self.request)

    def has_appointment_context(self):
        return bool(self.get_appointment_next_url())

    def get_return_url(self, client_id=None):
        if not self.has_appointment_context():
            return self.get_next_url() or reverse("core:client_list")
        return _appointment_create_url(
            target_day=self.get_selected_day(),
            slot_time=self.get_slot_time(),
            next_url=self.get_appointment_next_url(),
            client_id=client_id,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        selected_day = self.get_selected_day()
        slot_time = self.get_slot_time()
        appointment_context_label = ""
        has_appointment_context = self.has_appointment_context()

        if has_appointment_context and selected_day is not None:
            appointment_context_label = _format_day_title(selected_day)
            if slot_time:
                appointment_context_label = f"{appointment_context_label} · {slot_time}"
        elif has_appointment_context and slot_time:
            appointment_context_label = f"Tramo {slot_time}"

        context.update(
            {
                "page_title": "Nuevo cliente",
                "page_description": (
                    "Da de alta al cliente y vuelve al flujo de Nueva cita."
                    if has_appointment_context
                    else "Da de alta al cliente y sigue trabajando desde Clientes."
                ),
                "client_breadcrumbs": _client_breadcrumbs(
                    {"label": "Nuevo cliente", "url": ""}
                ),
                "back_url": self.get_return_url(),
                "back_label": (
                    "Volver a Nueva cita"
                    if has_appointment_context
                    else "Volver a clientes"
                ),
                "appointment_context_label": appointment_context_label,
                "return_year": selected_day.year if selected_day is not None else "",
                "return_month": selected_day.month if selected_day is not None else "",
                "return_day": selected_day.day if selected_day is not None else "",
                "return_slot_time": slot_time,
                "appointment_next_url": self.get_appointment_next_url(),
                "next_url": self.get_next_url(),
                "submit_label": "Guardar cliente",
            }
        )
        return context

    def form_valid(self, form):
        client = form.save()
        if self.has_appointment_context():
            return HttpResponseRedirect(self.get_return_url(client_id=client.pk))
        return HttpResponseRedirect(
            _url_with_next(
                reverse("core:client_detail", args=[client.pk]),
                self.get_return_url(),
            )
        )


class ClientUpdateView(AppLoginRequiredMixin, FormView):
    template_name = "core/client_form.html"
    form_class = ClientForm

    def get_client(self):
        if not hasattr(self, "_client"):
            self._client = get_object_or_404(Client.objects.active(), pk=self.kwargs["pk"])
        return self._client

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.get_client()
        return kwargs

    def get_success_url(self):
        next_url = _safe_next_url(self.request)
        if next_url:
            return next_url
        return reverse("core:client_detail", args=[self.get_client().pk])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        client = self.get_client()
        next_url = _safe_next_url(self.request)
        client_detail_url = _client_detail_navigation_url(client, next_url)
        context.update(
            {
                "page_title": "Editar cliente",
                "page_description": "Actualiza los datos base del cliente sin salir de la operativa.",
                "client_breadcrumbs": _client_breadcrumbs(
                    {"label": client.name, "url": client_detail_url},
                    {"label": "Editar", "url": ""},
                ),
                "back_url": next_url or reverse("core:client_detail", args=[client.pk]),
                "back_label": "Volver a ficha",
                "appointment_context_label": "",
                "return_year": "",
                "return_month": "",
                "return_day": "",
                "return_slot_time": "",
                "appointment_next_url": "",
                "next_url": next_url,
                "submit_label": "Guardar cambios",
            }
        )
        return context

    def form_valid(self, form):
        form.save()
        return HttpResponseRedirect(self.get_success_url())


class ArchivedClientListView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/archived_client_list.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        current_url = self.request.get_full_path()
        archived_clients = list(
            Client.objects.archived().annotate(appointment_count=Count("appointments")).order_by("name", "id")
        )
        for client in archived_clients:
            client.detail_url = _url_with_next(
                reverse("core:client_detail", args=[client.pk]),
                current_url,
            )
            client.reactivate_url = _url_with_next(
                reverse("core:client_reactivate", args=[client.pk]),
                current_url,
            )
            client.delete_url = _url_with_next(
                reverse("core:client_delete", args=[client.pk]),
                current_url,
            )
        context.update(
            {
                "archived_clients": archived_clients,
                "client_breadcrumbs": _client_breadcrumbs(
                    {"label": "Clientes archivados", "url": ""},
                ),
                "back_url": reverse("core:client_list"),
                "page_title": "Clientes archivados",
                "page_description": (
                    "Consulta clientes archivados y gestiona su reactivacion o eliminacion definitiva."
                ),
            }
        )
        return context


class ClientArchiveView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/client_archive_confirm.html"

    def get_client(self):
        if not hasattr(self, "_client"):
            self._client = get_object_or_404(Client.objects.active(), pk=self.kwargs["pk"])
        return self._client

    def get_back_url(self):
        return _safe_next_url(self.request) or reverse("core:client_detail", args=[self.get_client().pk])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        client = self.get_client()
        detail_url = _client_detail_navigation_url(client, self.get_back_url())
        future_active_appointments_count = client.appointments.filter(
            start_at__gte=timezone.now(),
            status__in=Appointment.active_statuses(),
        ).count()
        context.update(
            {
                "page_title": "Archivar cliente",
                "page_description": "Confirma si quieres sacar a este cliente de la operativa activa.",
                "client_obj": client,
                "client_breadcrumbs": _client_breadcrumbs(
                    {"label": client.name, "url": detail_url},
                    {"label": "Archivar", "url": ""},
                ),
                "back_url": self.get_back_url(),
                "future_active_appointments_count": future_active_appointments_count,
                "archived_client_list_url": _archived_client_list_url(),
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        self.get_client().archive()
        return HttpResponseRedirect(_archived_client_list_url())


class ClientReactivateView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/client_reactivate_confirm.html"

    def get_client(self):
        if not hasattr(self, "_client"):
            self._client = get_object_or_404(Client.objects.archived(), pk=self.kwargs["pk"])
        return self._client

    def get_back_url(self):
        return _safe_next_url(self.request) or _archived_client_list_url()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        client = self.get_client()
        back_url = self.get_back_url()
        detail_url = _client_detail_navigation_url(client, back_url)
        context.update(
            {
                "page_title": "Reactivar cliente",
                "page_description": "Confirma si quieres devolver este cliente a la operativa activa.",
                "client_obj": client,
                "client_breadcrumbs": _client_breadcrumbs(
                    {"label": "Clientes archivados", "url": _archived_client_list_url()},
                    {"label": client.name, "url": detail_url},
                    {"label": "Reactivar", "url": ""},
                ),
                "back_url": back_url,
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        self.get_client().reactivate()
        return HttpResponseRedirect(reverse("core:client_list"))


class ClientDeleteView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/client_delete_confirm.html"

    def get_client(self):
        if not hasattr(self, "_client"):
            self._client = get_object_or_404(Client.objects.archived(), pk=self.kwargs["pk"])
        return self._client

    def get_back_url(self):
        return _safe_next_url(self.request) or _archived_client_list_url()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        client = self.get_client()
        back_url = self.get_back_url()
        detail_url = _client_detail_navigation_url(client, back_url)
        context.update(
            {
                "page_title": "Eliminar definitivamente",
                "page_description": "Confirma la eliminacion total de este cliente archivado.",
                "client_obj": client,
                "client_breadcrumbs": _client_breadcrumbs(
                    {"label": "Clientes archivados", "url": _archived_client_list_url()},
                    {"label": client.name, "url": detail_url},
                    {"label": "Eliminar definitivamente", "url": ""},
                ),
                "back_url": back_url,
                "appointment_count": client.appointments.count(),
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        self.get_client().delete()
        return HttpResponseRedirect(_archived_client_list_url())


class ServiceSettingsView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/service_settings.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "back_url": _settings_index_url(),
                "settings_breadcrumbs": _settings_breadcrumbs(
                    {"label": "Servicios", "url": ""},
                ),
                "services": Service.objects.active().order_by("name", "id"),
                "service_create_url": reverse("core:service_create"),
            }
        )
        return context


class ServiceFormViewBase(AppLoginRequiredMixin, FormView):
    template_name = "core/service_form.html"
    form_class = ServiceForm

    def get_service(self):
        return None

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.get_service()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        service = self.get_service()
        is_edit = service is not None
        context.update(
            {
                "page_title": "Editar servicio" if is_edit else "Nuevo servicio",
                "page_description": "Define el nombre y la descripcion visible de este servicio.",
                "submit_label": "Guardar cambios" if is_edit else "Crear servicio",
                "back_url": _service_settings_url(),
                "is_edit": is_edit,
                "settings_breadcrumbs": _settings_breadcrumbs(
                    {"label": "Servicios", "url": _service_settings_url()},
                    {"label": "Editar servicio" if is_edit else "Nuevo servicio", "url": ""},
                ),
            }
        )
        return context

    def form_valid(self, form):
        service = form.save(commit=False)
        service.is_active = True
        service.save()
        return HttpResponseRedirect(_service_settings_url())


class ServiceCreateView(ServiceFormViewBase):
    pass


class ServiceUpdateView(ServiceFormViewBase):
    def get_service(self):
        if not hasattr(self, "_service"):
            self._service = get_object_or_404(Service, pk=self.kwargs["pk"], is_active=True)
        return self._service


class ServiceDeleteView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/service_confirm_delete.html"

    def get_service(self):
        if not hasattr(self, "_service"):
            self._service = get_object_or_404(Service, pk=self.kwargs["pk"], is_active=True)
        return self._service

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        service = self.get_service()
        context.update(
            {
                "page_title": "Eliminar servicio",
                "page_description": "Retira este servicio del catalogo operativo para nuevas citas.",
                "service": service,
                "back_url": _service_settings_url(),
                "settings_breadcrumbs": _settings_breadcrumbs(
                    {"label": "Servicios", "url": _service_settings_url()},
                    {"label": "Eliminar servicio", "url": ""},
                ),
            }
        )
        return context

    def post(self, request, *args, **kwargs):
        service = self.get_service()
        service.is_active = False
        service.save(update_fields=["is_active"])
        return HttpResponseRedirect(_service_settings_url())


class UIValidationView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/ui_preview.html"


class CalendarUIValidationView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/calendar_ui_preview.html"
