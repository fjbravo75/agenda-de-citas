from django.urls import path

from .views import AppEntryPointView


app_name = "core"

urlpatterns = [
    path("", AppEntryPointView.as_view(), name="app_entrypoint"),
]
