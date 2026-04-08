from django.urls import path

from .views import (
    AvailabilityBlockToggleView,
    AppEntryPointView,
    AppLogoutView,
    ClientDetailView,
    ClientCreateView,
    AppointmentCreateView,
    AppointmentUpdateView,
    CalendarUIValidationView,
    UIValidationView,
)


app_name = "core"

urlpatterns = [
    path("", AppEntryPointView.as_view(), name="app_entrypoint"),
    path("logout/", AppLogoutView.as_view(), name="app_logout"),
    path("availability-blocks/toggle/", AvailabilityBlockToggleView.as_view(), name="availability_block_toggle"),
    path("appointments/new/", AppointmentCreateView.as_view(), name="appointment_create"),
    path("appointments/<int:pk>/edit/", AppointmentUpdateView.as_view(), name="appointment_update"),
    path("clients/new/", ClientCreateView.as_view(), name="client_create"),
    path("clients/<int:pk>/", ClientDetailView.as_view(), name="client_detail"),
    path("ui/", UIValidationView.as_view(), name="ui_preview"),
    path("calendar-ui/", CalendarUIValidationView.as_view(), name="calendar_ui_preview"),
]
