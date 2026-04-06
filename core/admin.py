from django.contrib import admin

from .models import Appointment, AvailabilityBlock, Client, Service, WeeklyAvailability


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "email")
    search_fields = ("name", "phone", "email")


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "duration_minutes", "color")
    search_fields = ("name",)


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("start_at", "end_at", "status", "client", "service")
    list_filter = ("status", "service")
    search_fields = ("client__name", "service__name", "internal_notes")
    autocomplete_fields = ("client", "service")
    date_hierarchy = "start_at"


@admin.register(WeeklyAvailability)
class WeeklyAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("weekday", "slot_time", "capacity")
    list_filter = ("weekday",)
    search_fields = ("slot_time",)


@admin.register(AvailabilityBlock)
class AvailabilityBlockAdmin(admin.ModelAdmin):
    list_display = ("day", "slot_time", "label")
    list_filter = ("day",)
    search_fields = ("label", "slot_time")
