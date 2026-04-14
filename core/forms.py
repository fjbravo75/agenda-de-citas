from datetime import datetime, time

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from .day_availability import DayAvailabilityResolver
from .models import (
    AGENDA_SLOT_TIME_CHOICES,
    AgendaSettings,
    Appointment,
    BusinessSettings,
    Client,
    ManualClosure,
    Service,
    agenda_end_at_for_slot,
    agenda_slot_operational_state_map,
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


class ClientForm(forms.ModelForm):
    class Meta:
        model = Client
        fields = ("name", "phone", "email", "notes")
        labels = {
            "name": "Nombre",
            "phone": "Telefono",
            "email": "Email",
            "notes": "Notas",
        }
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 4}),
        }


class AgendaSettingsForm(forms.ModelForm):
    class Meta:
        model = AgendaSettings
        fields = ("saturdays_non_working", "sundays_non_working", "official_holidays_non_working")
        labels = {
            "saturdays_non_working": "Sabados no operativos",
            "sundays_non_working": "Domingos no operativos",
            "official_holidays_non_working": "Aplicar festivos oficiales como no operativos",
        }
        help_texts = {
            "saturdays_non_working": "Activa este ajuste si la parrilla fija no debe abrirse los sabados.",
            "sundays_non_working": "Activa este ajuste si la parrilla fija no debe abrirse los domingos.",
            "official_holidays_non_working": (
                "Si lo desactivas, los festivos oficiales seguiran visibles como dato, "
                "pero no cerraran operativamente la agenda."
            ),
        }


class BusinessSettingsForm(forms.ModelForm):
    class Meta:
        model = BusinessSettings
        fields = ("business_name", "phone", "email", "address", "city", "tax_id")
        labels = {
            "business_name": "Nombre del negocio",
            "phone": "Telefono",
            "email": "Email",
            "address": "Direccion",
            "city": "Ciudad o localidad",
            "tax_id": "NIF/CIF",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in self.fields:
            self.fields[field_name].required = True

    def clean_business_name(self):
        return " ".join(self.cleaned_data["business_name"].split())

    def clean_phone(self):
        return self.cleaned_data["phone"].strip()

    def clean_email(self):
        return self.cleaned_data["email"].strip().lower()

    def clean_address(self):
        return " ".join(self.cleaned_data["address"].split())

    def clean_city(self):
        return " ".join(self.cleaned_data["city"].split())

    def clean_tax_id(self):
        return self.cleaned_data["tax_id"].strip().upper()


class ManualClosureForm(forms.ModelForm):
    class Meta:
        model = ManualClosure
        fields = ("start_date", "end_date", "reason_type", "label", "notes")
        labels = {
            "start_date": "Fecha inicial",
            "end_date": "Fecha final",
            "reason_type": "Motivo",
            "label": "Etiqueta visible",
            "notes": "Notas internas",
        }
        help_texts = {
            "label": "Opcional. Si lo dejas vacio, se mostrara el motivo elegido.",
            "notes": "Opcional. Uso interno para dar contexto al cierre.",
        }
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 4}),
        }


class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ("name", "description")
        labels = {
            "name": "Nombre",
            "description": "Descripcion",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
        }


class OfficialHolidaySyncForm(forms.Form):
    year = forms.IntegerField(
        label="Año",
        min_value=2000,
        max_value=2100,
        help_text="Importa los festivos nacionales de España publicados en el BOE para el año indicado.",
    )


class AppointmentForm(forms.Form):
    client = forms.ModelChoiceField(queryset=Client.objects.none(), label="Cliente")
    services = forms.ModelMultipleChoiceField(
        queryset=Service.objects.none(),
        label="Servicios",
        widget=forms.CheckboxSelectMultiple,
    )
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

    def __init__(
        self,
        *args,
        instance=None,
        initial_day=None,
        initial_slot_time=None,
        initial_client_id=None,
        initial_service_ids=None,
        **kwargs,
    ):
        self.instance = instance or Appointment()
        self._selected_services = []
        super().__init__(*args, **kwargs)
        self.fields["client"].queryset = self._clients_queryset()
        self.fields["services"].queryset = self._services_queryset()
        self._configure_status_field()
        target_day = self._resolve_target_day(initial_day)
        self._configure_slot_field(target_day)

        if self.is_bound:
            return

        if self.instance.pk:
            self.initial.update(
                {
                    "client": self.instance.client_id,
                    "services": list(self.instance.services.values_list("pk", flat=True)),
                    "day": self.instance.slot_day,
                    "slot_time": self.instance.slot_time,
                    "status": self.instance.status,
                    "internal_notes": self.instance.internal_notes,
                }
            )
        elif initial_day is not None:
            self.initial.setdefault("day", initial_day)
            self.initial.setdefault("status", Appointment.Status.PENDING)

        initial_client = self._resolve_initial_client(initial_client_id)
        if initial_client is not None:
            self.initial.setdefault("client", initial_client.pk)

        initial_services = self._resolve_initial_services(initial_service_ids)
        if initial_services:
            self.initial.setdefault("services", [service.pk for service in initial_services])

        if self._slot_is_bookable(initial_slot_time):
            self.initial.setdefault("slot_time", initial_slot_time)

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
        self.instance.services.set(self._selected_services)
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
        day_availability = DayAvailabilityResolver.resolve_for_global_agenda(target_day)
        slot_state_map = agenda_slot_operational_state_map(
            target_day,
            exclude_pk=self.instance.pk if self.instance.pk else None,
        )
        slot_choices = []
        disabled_values = set()

        for slot_time, _ in AGENDA_SLOT_TIME_CHOICES:
            slot_snapshot = slot_state_map.get(slot_time, {})
            slot_choices.append(
                (
                    slot_time,
                    self._slot_choice_label(
                        slot_time,
                        slot_snapshot,
                        day_availability=day_availability,
                    ),
                )
            )
            if not self._slot_is_selectable_for_day(
                target_day,
                slot_time,
                slot_snapshot,
                day_availability,
            ):
                disabled_values.add(slot_time)

        self.fields["slot_time"].choices = slot_choices
        self.fields["slot_time"].help_text = "Solo se pueden elegir tramos con plaza libre dentro de su capacidad."
        self.fields["slot_time"].widget.choices = slot_choices
        self.fields["slot_time"].widget.disabled_values = disabled_values

    def _configure_status_field(self):
        if self.instance.pk:
            status_choices = Appointment.Status.choices
        else:
            status_choices = [
                choice
                for choice in Appointment.Status.choices
                if choice[0] != Appointment.Status.CANCELLED
            ]
        self.fields["status"].choices = status_choices

    def _services_queryset(self):
        if self.instance.pk:
            current_service_ids = self.instance.services.values_list("pk", flat=True)
            return Service.objects.filter(
                Q(is_active=True) | Q(pk__in=current_service_ids)
            ).distinct().order_by("name", "id")
        return Service.objects.active().order_by("name", "id")

    def _clients_queryset(self):
        if self.instance.pk and self.instance.client_id:
            return Client.objects.filter(
                Q(is_archived=False) | Q(pk=self.instance.client_id)
            ).distinct().order_by("name", "id")
        return Client.objects.active().order_by("name", "id")

    def _first_bookable_slot(self, target_day):
        day_availability = DayAvailabilityResolver.resolve_for_global_agenda(target_day)
        if not day_availability.is_working_day:
            return None

        slot_state_map = agenda_slot_operational_state_map(
            target_day,
            exclude_pk=self.instance.pk if self.instance.pk else None,
        )
        for slot_time, _ in AGENDA_SLOT_TIME_CHOICES:
            if slot_state_map.get(slot_time, {}).get("can_book"):
                return slot_time
        return None

    def _slot_choice_label(self, slot_time, slot_snapshot, *, day_availability):
        if not day_availability.is_working_day:
            suffix = day_availability.label
            return f"{slot_time} · {suffix}"

        if slot_snapshot.get("blocked_label"):
            suffix = slot_snapshot["blocked_label"]
        elif slot_snapshot.get("is_complete"):
            suffix = "Completo"
        elif slot_snapshot.get("active_count"):
            capacity = slot_snapshot.get("capacity")
            occupied_label = "ocupada" if slot_snapshot["active_count"] == 1 else "ocupadas"
            suffix = f"{slot_snapshot['active_count']}/{capacity} {occupied_label}"
        else:
            suffix = "Disponible"
        return f"{slot_time} · {suffix}"

    def _slot_is_selectable_for_day(self, target_day, slot_time, slot_snapshot, day_availability):
        if day_availability.is_working_day:
            return slot_snapshot.get("can_book")
        return self._slot_matches_existing_assignment(target_day, slot_time)

    def _slot_matches_existing_assignment(self, target_day, slot_time):
        return (
            bool(self.instance.pk)
            and self.instance.slot_day == target_day
            and self.instance.slot_time == slot_time
        )

    def _slot_is_bookable(self, slot_time):
        if not slot_time:
            return False
        valid_values = {str(value) for value, _ in self.fields["slot_time"].choices}
        return slot_time in valid_values and slot_time not in self.fields["slot_time"].widget.disabled_values

    def _resolve_initial_client(self, initial_client_id):
        if self.instance.pk or not initial_client_id:
            return None

        try:
            normalized_client_id = int(initial_client_id)
        except (TypeError, ValueError):
            return None

        try:
            return self.fields["client"].queryset.get(pk=normalized_client_id)
        except Client.DoesNotExist:
            return None

    def _resolve_initial_services(self, initial_service_ids):
        if self.instance.pk or not initial_service_ids:
            return []

        normalized_service_ids = []
        for service_id in initial_service_ids:
            try:
                normalized_service_ids.append(int(service_id))
            except (TypeError, ValueError):
                continue

        if not normalized_service_ids:
            return []

        services_by_id = {
            service.pk: service
            for service in self.fields["services"].queryset.filter(pk__in=normalized_service_ids)
        }
        return [services_by_id[service_id] for service_id in normalized_service_ids if service_id in services_by_id]

    def _assign_instance_values(self, cleaned_data):
        client = cleaned_data.get("client")
        services = list(cleaned_data.get("services") or [])
        day = cleaned_data.get("day")
        slot_time = cleaned_data.get("slot_time")
        status = cleaned_data.get("status")
        internal_notes = cleaned_data.get("internal_notes", "")

        if not all([client, services, day, slot_time, status]):
            return

        start_at = timezone.make_aware(
            datetime.combine(day, time.fromisoformat(slot_time)),
            timezone.get_current_timezone(),
        )
        self.instance.client = client
        self.instance.start_at = start_at
        self.instance.end_at = agenda_end_at_for_slot(start_at)
        self.instance.status = status
        self.instance.internal_notes = internal_notes
        self._selected_services = services

    def _apply_model_errors(self, error):
        if hasattr(error, "error_dict"):
            for field_name, messages in error.message_dict.items():
                if field_name == "start_at":
                    target_field = "slot_time"
                elif field_name == "status":
                    target_field = "status"
                else:
                    target_field = None
                for message in messages:
                    self.add_error(target_field, message)
            return

        for message in error.messages:
            self.add_error(None, message)
