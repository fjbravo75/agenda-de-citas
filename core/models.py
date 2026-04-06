from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


HEX_COLOR_VALIDATOR = RegexValidator(
    regex=r"^#(?:[0-9A-Fa-f]{6})$",
    message="Use a full hex color like #3158D7.",
)

AGENDA_SLOT_TIMES = ("09:00", "10:00", "11:00", "12:00", "13:00", "16:00", "17:00", "18:00")
AGENDA_SLOT_TIME_CHOICES = tuple((slot_time, slot_time) for slot_time in AGENDA_SLOT_TIMES)
AGENDA_SLOT_VALUES = tuple(time.fromisoformat(slot_time) for slot_time in AGENDA_SLOT_TIMES)
DEFAULT_SLOT_CAPACITY = 2


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Lunes"
    TUESDAY = 1, "Martes"
    WEDNESDAY = 2, "Miercoles"
    THURSDAY = 3, "Jueves"
    FRIDAY = 4, "Viernes"
    SATURDAY = 5, "Sabado"
    SUNDAY = 6, "Domingo"


def _current_timezone():
    return timezone.get_current_timezone()


def agenda_day_bounds(target_day):
    start_at = timezone.make_aware(datetime.combine(target_day, time.min), _current_timezone())
    return start_at, start_at + timedelta(days=1)


def agenda_local_slot_time(target_datetime):
    if target_datetime is None:
        return ""
    local_time = timezone.localtime(target_datetime).time().replace(second=0, microsecond=0)
    return local_time.strftime("%H:%M")


def agenda_assigned_slot_time(target_datetime):
    if target_datetime is None:
        return ""

    exact_slot_time = agenda_local_slot_time(target_datetime)
    if exact_slot_time in AGENDA_SLOT_TIMES:
        return exact_slot_time

    local_time = timezone.localtime(target_datetime).time().replace(second=0, microsecond=0)
    selected_slot = AGENDA_SLOT_TIMES[0]
    for index, slot_time_value in enumerate(AGENDA_SLOT_VALUES):
        if local_time < slot_time_value:
            return AGENDA_SLOT_TIMES[index - 1] if index > 0 else selected_slot
        selected_slot = AGENDA_SLOT_TIMES[index]
    return selected_slot


def agenda_slot_day(target_datetime):
    if target_datetime is None:
        return None
    return timezone.localtime(target_datetime).date()


class Client(models.Model):
    name = models.CharField(max_length=160)
    phone = models.CharField(max_length=32, blank=True)
    email = models.EmailField(blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("name", "id")

    def __str__(self):
        return self.name


class Service(models.Model):
    name = models.CharField(max_length=120)
    duration_minutes = models.PositiveIntegerField(validators=[MinValueValidator(5)])
    color = models.CharField(max_length=7, blank=True, validators=[HEX_COLOR_VALIDATOR])

    class Meta:
        ordering = ("name", "id")

    def __str__(self):
        return self.name


class Appointment(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Pendiente"
        CONFIRMED = "confirmed", "Confirmada"
        CANCELLED = "cancelled", "Cancelada"

    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="appointments")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="appointments")
    start_at = models.DateTimeField(db_index=True)
    end_at = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True)
    internal_notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("start_at", "id")
        indexes = [
            models.Index(fields=("start_at", "status")),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(end_at__gt=F("start_at")),
                name="core_appointment_end_after_start",
            ),
        ]

    def __str__(self):
        return f"{self.client} - {self.service} @ {self.start_at:%Y-%m-%d %H:%M}"

    @classmethod
    def active_statuses(cls):
        return (cls.Status.PENDING, cls.Status.CONFIRMED)

    @property
    def slot_time(self):
        return agenda_assigned_slot_time(self.start_at)

    @property
    def slot_day(self):
        return agenda_slot_day(self.start_at)

    @classmethod
    def active_slot_appointments_count(cls, target_day, slot_time, exclude_pk=None):
        day_start, day_end = agenda_day_bounds(target_day)
        queryset = cls.objects.filter(
            start_at__gte=day_start,
            start_at__lt=day_end,
            status__in=cls.active_statuses(),
        ).only("id", "start_at")

        if exclude_pk is not None:
            queryset = queryset.exclude(pk=exclude_pk)

        return sum(1 for appointment in queryset if agenda_assigned_slot_time(appointment.start_at) == slot_time)

    def clean(self):
        super().clean()

        if self.start_at is None:
            return

        errors = {}
        exact_slot_time = agenda_local_slot_time(self.start_at)

        if exact_slot_time not in AGENDA_SLOT_TIMES:
            errors["start_at"] = "La cita debe asignarse a uno de los tramos fijos de la agenda."

        if self.status == self.Status.CANCELLED:
            if errors:
                raise ValidationError(errors)
            return

        if errors:
            raise ValidationError(errors)

        target_day = agenda_slot_day(self.start_at)
        availability = WeeklyAvailability.objects.filter(
            weekday=target_day.weekday(),
            slot_time=exact_slot_time,
        ).first()

        if availability is None:
            errors["start_at"] = "El tramo seleccionado queda fuera de la disponibilidad."
        elif AvailabilityBlock.objects.filter(day=target_day, slot_time=exact_slot_time).exists():
            errors["start_at"] = "El tramo seleccionado esta bloqueado."
        else:
            occupied_slots = self.active_slot_appointments_count(
                target_day,
                exact_slot_time,
                exclude_pk=self.pk,
            )
            if occupied_slots >= availability.capacity:
                errors["start_at"] = "El tramo seleccionado ya ha alcanzado su capacidad maxima."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class WeeklyAvailability(models.Model):
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    slot_time = models.CharField(max_length=5, choices=AGENDA_SLOT_TIME_CHOICES)
    capacity = models.PositiveSmallIntegerField(
        default=DEFAULT_SLOT_CAPACITY,
        validators=[MinValueValidator(1)],
    )

    class Meta:
        ordering = ("weekday", "slot_time", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("weekday", "slot_time"),
                name="core_weeklyavailability_unique_slot",
            ),
        ]

    def __str__(self):
        return f"{Weekday(self.weekday).label} {self.slot_time} · capacidad {self.capacity}"


class AvailabilityBlock(models.Model):
    day = models.DateField(db_index=True)
    slot_time = models.CharField(max_length=5, choices=AGENDA_SLOT_TIME_CHOICES)
    label = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ("day", "slot_time", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("day", "slot_time"),
                name="core_availabilityblock_unique_slot",
            ),
        ]

    def __str__(self):
        display_label = self.label or "Bloqueo puntual"
        return f"{self.day:%Y-%m-%d} {self.slot_time} - {display_label}"
