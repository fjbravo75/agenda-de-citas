from django.contrib import admin

from .models import Appointment, Client, Service


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
