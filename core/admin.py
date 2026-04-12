from django.contrib import admin

from .models import Appointment, AvailabilityBlock, Client, Service


@admin.register(Client)
class ClientAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "email")
    search_fields = ("name", "phone", "email")


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ("name", "color", "is_active")
    search_fields = ("name",)


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("start_at", "end_at", "status", "client", "services_summary")
    list_filter = ("status", "services")
    search_fields = ("client__name", "services__name", "internal_notes")
    autocomplete_fields = ("client", "services")
    date_hierarchy = "start_at"

    @admin.display(description="Servicios")
    def services_summary(self, obj):
        return obj.services_label or "Sin servicios"


@admin.register(AvailabilityBlock)
class AvailabilityBlockAdmin(admin.ModelAdmin):
    list_display = ("day", "slot_time", "label")
    list_filter = ("day",)
    search_fields = ("label", "slot_time")
