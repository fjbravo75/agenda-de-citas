from datetime import datetime, time, timedelta

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import (
    AGENDA_SLOT_TIME_CHOICES,
    Appointment,
    Client,
    Service,
    agenda_slot_booking_state,
)


class SlotChoiceSelect(forms.Select):
    def __init__(self, *args, **kwargs):
        self.disabled_values = set()
        super().__init__(*args, **kwargs)

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        normalized_value = "" if value is None else str(value)
        if normalized_value in self.disabled_values:
            option["attrs"]["disabled"] = True
        return option


class AppointmentForm(forms.Form):
    client = forms.ModelChoiceField(queryset=Client.objects.none(), label="Cliente")
    service = forms.ModelChoiceField(queryset=Service.objects.none(), label="Servicio")
    day = forms.DateField(
        label="Fecha",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    slot_time = forms.ChoiceField(
        choices=AGENDA_SLOT_TIME_CHOICES,
        label="Tramo",
        widget=SlotChoiceSelect(),
    )
    status = forms.ChoiceField(choices=Appointment.Status.choices, label="Estado")
    internal_notes = forms.CharField(
        label="Notas internas",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    def __init__(self, *args, instance=None, initial_day=None, **kwargs):
        self.instance = instance or Appointment()
        super().__init__(*args, **kwargs)
        self.fields["client"].queryset = Client.objects.order_by("name", "id")
        self.fields["service"].queryset = Service.objects.order_by("name", "id")
        target_day = self._resolve_target_day(initial_day)
        self._configure_slot_field(target_day)

        if self.is_bound:
            return

        if self.instance.pk:
            self.initial.update(
                {
                    "client": self.instance.client_id,
                    "service": self.instance.service_id,
                    "day": self.instance.slot_day,
                    "slot_time": self.instance.slot_time,
                    "status": self.instance.status,
                    "internal_notes": self.instance.internal_notes,
                }
            )
        elif initial_day is not None:
            self.initial.setdefault("day", initial_day)
            self.initial.setdefault("status", Appointment.Status.PENDING)

        first_bookable_slot = self._first_bookable_slot(target_day)
        if first_bookable_slot is not None:
            self.initial.setdefault("slot_time", first_bookable_slot)

    def clean(self):
        cleaned_data = super().clean()
        if self.errors:
            return cleaned_data

        self._assign_instance_values(cleaned_data)

        try:
            self.instance.full_clean()
        except ValidationError as error:
            self._apply_model_errors(error)

        return cleaned_data

    def save(self):
        self.instance.save()
        return self.instance

    def _resolve_target_day(self, initial_day):
        if self.is_bound:
            raw_day = self.data.get(self.add_prefix("day"))
            if raw_day:
                try:
                    return self.fields["day"].to_python(raw_day)
                except ValidationError:
                    pass

        if self.instance.pk and self.instance.slot_day is not None:
            return self.instance.slot_day

        if initial_day is not None:
            return initial_day

        return timezone.localdate()

    def _configure_slot_field(self, target_day):
        slot_states = agenda_slot_booking_state(
            target_day,
            exclude_pk=self.instance.pk if self.instance.pk else None,
        )
        slot_choices = []
        disabled_values = set()

        for slot_time, _ in AGENDA_SLOT_TIME_CHOICES:
            slot_state = slot_states.get(slot_time, {})
            slot_choices.append((slot_time, self._slot_choice_label(slot_time, slot_state)))
            if not slot_state.get("can_book"):
                disabled_values.add(slot_time)

        self.fields["slot_time"].choices = slot_choices
        self.fields["slot_time"].help_text = "Solo se pueden elegir tramos con plaza libre dentro de su capacidad."
        self.fields["slot_time"].widget.choices = slot_choices
        self.fields["slot_time"].widget.disabled_values = disabled_values

    def _first_bookable_slot(self, target_day):
        slot_states = agenda_slot_booking_state(
            target_day,
            exclude_pk=self.instance.pk if self.instance.pk else None,
        )
        for slot_time, _ in AGENDA_SLOT_TIME_CHOICES:
            if slot_states.get(slot_time, {}).get("can_book"):
                return slot_time
        return None

    def _slot_choice_label(self, slot_time, slot_state):
        if slot_state.get("blocked_label"):
            suffix = slot_state["blocked_label"]
        elif not slot_state.get("is_within_availability"):
            suffix = "Fuera de disponibilidad"
        elif slot_state.get("is_complete"):
            suffix = "Completo"
        elif slot_state.get("active_count"):
            capacity = slot_state.get("capacity")
            occupied_label = "ocupada" if slot_state["active_count"] == 1 else "ocupadas"
            suffix = f"{slot_state['active_count']}/{capacity} {occupied_label}"
        else:
            suffix = "Disponible"
        return f"{slot_time} · {suffix}"

    def _assign_instance_values(self, cleaned_data):
        client = cleaned_data.get("client")
        service = cleaned_data.get("service")
        day = cleaned_data.get("day")
        slot_time = cleaned_data.get("slot_time")
        status = cleaned_data.get("status")
        internal_notes = cleaned_data.get("internal_notes", "")

        if not all([client, service, day, slot_time, status]):
            return

        start_at = timezone.make_aware(
            datetime.combine(day, time.fromisoformat(slot_time)),
            timezone.get_current_timezone(),
        )

        self.instance.client = client
        self.instance.service = service
        self.instance.start_at = start_at
        self.instance.end_at = start_at + timedelta(minutes=service.duration_minutes)
        self.instance.status = status
        self.instance.internal_notes = internal_notes

    def _apply_model_errors(self, error):
        if hasattr(error, "error_dict"):
            for field_name, messages in error.message_dict.items():
                target_field = "slot_time" if field_name == "start_at" else None
                for message in messages:
                    self.add_error(target_field, message)
            return

        for message in error.messages:
            self.add_error(None, message)
