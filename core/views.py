from calendar import Calendar, monthrange
from datetime import date, datetime, time, timedelta
from urllib.parse import urlencode

from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils import timezone
from django.views.generic import FormView, TemplateView
from wagtail.admin.views.account import LogoutView as WagtailLogoutView

from .forms import AppointmentForm, ClientForm
from .models import (
    AGENDA_SLOT_TIMES,
    Appointment,
    AvailabilityBlock,
    Client,
    agenda_assigned_slot_time,
    agenda_slot_booking_state,
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


def _app_oriented_login_url():
    return f"{reverse('wagtailadmin_login')}?{urlencode({'next': reverse('core:app_entrypoint')})}"


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


def _format_compact_day(target_day):
    return f"{target_day.day} {MONTH_NAMES[target_day.month]} {target_day.year}"


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


def _build_timeline_slots(target_day, appointments, return_url=""):
    slots = _empty_timeline_slots()
    slot_map = {slot["time"]: slot for slot in slots}
    slot_state = agenda_slot_booking_state(target_day)

    for appointment in appointments:
        slot_map[agenda_assigned_slot_time(appointment.start_at)]["entries"].append(
            _build_entry_from_appointment(appointment, target_day)
        )

    for slot in slots:
        state = slot_state[slot["time"]]
        slot["active_entries_count"] = state["active_count"]
        slot["capacity"] = state["capacity"]
        slot["can_book"] = state["can_book"]

        if state["can_book"]:
            slot["create_url"] = _appointment_create_url_for_slot(
                target_day,
                slot["time"],
                next_url=return_url or _agenda_url_for_day(target_day),
            )
            slot["create_label"] = "Nueva cita"

        if slot["entries"]:
            if state["is_complete"]:
                slot["complete_label"] = "Completo"
            elif state["active_count"]:
                slot["busy_label"] = _format_busy_slot_label(state["active_count"], state["capacity"])
            else:
                slot["busy_label"] = "Sin ocupacion activa"
            continue

        if state["blocked_label"]:
            slot["blocked_label"] = state["blocked_label"]
            continue

        if state["is_within_availability"]:
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


def _build_day_panel(selected_day, return_url=""):
    appointments = _appointments_for_day(selected_day)
    timeline_slots = _build_timeline_slots(selected_day, appointments, return_url=return_url)
    return {
        "selected_day_title": _format_day_title(selected_day),
        "selected_day_summary": _build_day_summary(timeline_slots),
        "agenda_timeline_slots": timeline_slots,
    }


def _cancelled_appointments_count_for_day(target_day):
    start_at, end_at = _day_bounds(target_day)
    return Appointment.objects.filter(
        start_at__gte=start_at,
        start_at__lt=end_at,
        status=Appointment.Status.CANCELLED,
    ).count()


def _build_agenda_metrics(timeline_slots, selected_day):
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
    cancelled_entries = _cancelled_appointments_count_for_day(selected_day)

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
            "meta": "sin ocupar tramo activo",
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


class AppLoginRequiredMixin(LoginRequiredMixin):
    login_url = reverse_lazy("wagtailadmin_login")


class AppLogoutView(WagtailLogoutView):
    @property
    def next_page(self):
        return _app_oriented_login_url()


class AppEntryPointView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/app_entrypoint.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        visible_year, visible_month, selected_day, real_today = _resolve_calendar_state(self.request)
        day_panel = _build_day_panel(selected_day, return_url=self.request.get_full_path())
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
                "agenda_metrics": _build_agenda_metrics(today_panel["agenda_timeline_slots"], selected_day),
                "today_context_label": _format_today_context_label(real_today),
            }
        )
        context.update(day_panel)
        return context


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
        return kwargs

    def get_initial_slot_time(self):
        raw_slot_time = self.request.GET.get("slot_time", "")
        if raw_slot_time in AGENDA_SLOT_TIMES:
            return raw_slot_time
        return None

    def get_initial_client_id(self):
        raw_client_id = self.request.GET.get("client", "").strip()
        return raw_client_id or None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        appointment = self.get_appointment()
        selected_day = self.get_selected_day()
        is_edit = appointment is not None
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
                    "Ajusta cliente, servicio, fecha y tramo. La validacion sigue la disponibilidad,"
                    " los bloqueos y la capacidad real del tramo."
                ),
                "submit_label": "Guardar cambios" if is_edit else "Guardar cita",
                "back_url": _safe_next_url(self.request) or _agenda_url_for_day(selected_day),
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
            self._appointment = get_object_or_404(Appointment, pk=self.kwargs["pk"])
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
        month_summary = _month_summary(visible_year, visible_month)
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


class ClientDetailView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/client_detail.html"

    def get_client(self):
        if not hasattr(self, "_client"):
            self._client = get_object_or_404(Client, pk=self.kwargs["pk"])
        return self._client

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        client = self.get_client()
        next_url = _safe_next_url(self.request)
        fallback_url = reverse("core:app_entrypoint")
        history_items = []

        appointments = client.appointments.select_related("service").order_by("-start_at", "-id")
        for appointment in appointments:
            slot_day = appointment.slot_day
            edit_url = reverse("core:appointment_update", args=[appointment.pk])
            if slot_day is not None:
                edit_url = f"{edit_url}?{_query_string(slot_day.year, slot_day.month, slot_day.day)}"
            history_items.append(
                {
                    "date_label": _format_compact_day(slot_day) if slot_day is not None else "",
                    "slot_time": appointment.slot_time,
                    "service_label": appointment.service.name,
                    "status_label": appointment.get_status_display(),
                    "status_key": appointment.status,
                    "edit_url": edit_url,
                }
            )

        context.update(
            {
                "client_obj": client,
                "client_history": history_items,
                "back_url": next_url or fallback_url,
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

    def get_return_url(self, client_id=None):
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

        if selected_day is not None:
            appointment_context_label = _format_day_title(selected_day)
            if slot_time:
                appointment_context_label = f"{appointment_context_label} · {slot_time}"
        elif slot_time:
            appointment_context_label = f"Tramo {slot_time}"

        context.update(
            {
                "page_title": "Nuevo cliente",
                "page_description": "Da de alta al cliente y vuelve al flujo de Nueva cita.",
                "back_url": self.get_return_url(),
                "appointment_context_label": appointment_context_label,
                "return_year": selected_day.year if selected_day is not None else "",
                "return_month": selected_day.month if selected_day is not None else "",
                "return_day": selected_day.day if selected_day is not None else "",
                "return_slot_time": slot_time,
                "appointment_next_url": self.get_appointment_next_url(),
            }
        )
        return context

    def form_valid(self, form):
        client = form.save()
        return HttpResponseRedirect(self.get_return_url(client_id=client.pk))


class UIValidationView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/ui_preview.html"


class CalendarUIValidationView(AppLoginRequiredMixin, TemplateView):
    template_name = "core/calendar_ui_preview.html"
