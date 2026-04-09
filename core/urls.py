from django.urls import path

from .views import (
    AgendaSettingsView,
    AvailabilityBlockToggleView,
    AppEntryPointView,
    AppLogoutView,
    AppointmentCreateView,
    AppointmentUpdateView,
    CalendarUIValidationView,
    ClientCreateView,
    ClientDetailView,
    ManualClosureCreateView,
    ManualClosureDeleteView,
    ManualClosureUpdateView,
    UIValidationView,
)


app_name = "core"

urlpatterns = [
    path("", AppEntryPointView.as_view(), name="app_entrypoint"),
    path("logout/", AppLogoutView.as_view(), name="app_logout"),
    path("settings/agenda/", AgendaSettingsView.as_view(), name="agenda_settings"),
    path(
        "settings/agenda/closures/new/",
        ManualClosureCreateView.as_view(),
        name="manual_closure_create",
    ),
    path(
        "settings/agenda/closures/<int:pk>/edit/",
        ManualClosureUpdateView.as_view(),
        name="manual_closure_update",
    ),
    path(
        "settings/agenda/closures/<int:pk>/delete/",
        ManualClosureDeleteView.as_view(),
        name="manual_closure_delete",
    ),
    path("availability-blocks/toggle/", AvailabilityBlockToggleView.as_view(), name="availability_block_toggle"),
    path("appointments/new/", AppointmentCreateView.as_view(), name="appointment_create"),
    path("appointments/<int:pk>/edit/", AppointmentUpdateView.as_view(), name="appointment_update"),
    path("clients/new/", ClientCreateView.as_view(), name="client_create"),
    path("clients/<int:pk>/", ClientDetailView.as_view(), name="client_detail"),
    path("ui/", UIValidationView.as_view(), name="ui_preview"),
    path("calendar-ui/", CalendarUIValidationView.as_view(), name="calendar_ui_preview"),
]
