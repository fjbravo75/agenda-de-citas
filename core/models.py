from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import F, Q


HEX_COLOR_VALIDATOR = RegexValidator(
    regex=r"^#(?:[0-9A-Fa-f]{6})$",
    message="Use a full hex color like #3158D7.",
)


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
