from datetime import datetime, time, timedelta

from django import forms
from django.core.exceptions import ValidationError
from django.utils import timezone

from .models import AGENDA_SLOT_TIME_CHOICES, Appointment, Client, Service


class AppointmentForm(forms.Form):
    client = forms.ModelChoiceField(queryset=Client.objects.none(), label="Cliente")
    service = forms.ModelChoiceField(queryset=Service.objects.none(), label="Servicio")
    day = forms.DateField(
        label="Fecha",
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    slot_time = forms.ChoiceField(choices=AGENDA_SLOT_TIME_CHOICES, label="Tramo")
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
