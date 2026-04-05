from django.urls import path

from .views import AppEntryPointView, CalendarUIValidationView, UIValidationView


app_name = "core"

urlpatterns = [
    path("", AppEntryPointView.as_view(), name="app_entrypoint"),
    path("ui/", UIValidationView.as_view(), name="ui_preview"),
    path("calendar-ui/", CalendarUIValidationView.as_view(), name="calendar_ui_preview"),
]
