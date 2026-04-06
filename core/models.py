from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q


HEX_COLOR_VALIDATOR = RegexValidator(
    regex=r"^#(?:[0-9A-Fa-f]{6})$",
    message="Use a full hex color like #3158D7.",
)

AGENDA_SLOT_TIMES = ("09:00", "10:00", "11:00", "12:00", "13:00", "16:00", "17:00", "18:00")
AGENDA_SLOT_TIME_CHOICES = tuple((slot_time, slot_time) for slot_time in AGENDA_SLOT_TIMES)


class Weekday(models.IntegerChoices):
    MONDAY = 0, "Lunes"
    TUESDAY = 1, "Martes"
    WEDNESDAY = 2, "Miercoles"
    THURSDAY = 3, "Jueves"
    FRIDAY = 4, "Viernes"
    SATURDAY = 5, "Sabado"
    SUNDAY = 6, "Domingo"


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


class WeeklyAvailability(models.Model):
    weekday = models.PositiveSmallIntegerField(choices=Weekday.choices)
    slot_time = models.CharField(max_length=5, choices=AGENDA_SLOT_TIME_CHOICES)

    class Meta:
        ordering = ("weekday", "slot_time", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("weekday", "slot_time"),
                name="core_weeklyavailability_unique_slot",
            ),
        ]

    def __str__(self):
        return f"{Weekday(self.weekday).label} {self.slot_time}"


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
