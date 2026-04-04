from django.views.generic import TemplateView


class AppEntryPointView(TemplateView):
    template_name = "core/app_entrypoint.html"
