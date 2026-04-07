from django.urls import path

from .views import (
    AppEntryPointView,
    ClientDetailView,
    AppointmentCreateView,
    AppointmentUpdateView,
    CalendarUIValidationView,
    UIValidationView,
)


app_name = "core"

urlpatterns = [
    path("", AppEntryPointView.as_view(), name="app_entrypoint"),
    path("appointments/new/", AppointmentCreateView.as_view(), name="appointment_create"),
    path("appointments/<int:pk>/edit/", AppointmentUpdateView.as_view(), name="appointment_update"),
    path("clients/<int:pk>/", ClientDetailView.as_view(), name="client_detail"),
    path("ui/", UIValidationView.as_view(), name="ui_preview"),
    path("calendar-ui/", CalendarUIValidationView.as_view(), name="calendar_ui_preview"),
]
