from django.views.generic import TemplateView


AGENDA_PHASE_ONE_CONTEXT = {
    "agenda_metrics": [
        {
            "label": "Citas hoy",
            "value": "04",
            "meta": "agenda prevista hasta las 17:00",
        },
        {
            "label": "Huecos libres",
            "value": "02",
            "meta": "11:00 a 12:00 y 16:00 a 17:00",
        },
        {
            "label": "Proximas confirmadas",
            "value": "03",
            "meta": "incluyendo la apertura de las 09:00",
        },
        {
            "label": "Bloqueos activos",
            "value": "01",
            "meta": "parcial manana por la tarde",
        },
    ],
    "agenda_weeks": [
        [
            {"number": 30, "outside": True, "today": False, "selected": False, "markers": []},
            {"number": 31, "outside": True, "today": False, "selected": False, "markers": []},
            {
                "number": 1,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "2 citas", "kind": "busy"},
                ],
            },
            {
                "number": 2,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "4 citas", "kind": "busy"},
                ],
            },
            {"number": 3, "outside": False, "today": False, "selected": False, "markers": []},
            {"number": 4, "outside": False, "today": False, "selected": False, "markers": []},
            {"number": 5, "outside": False, "today": False, "selected": False, "markers": []},
        ],
        [
            {"number": 6, "outside": False, "today": False, "selected": False, "markers": []},
            {
                "number": 7,
                "outside": False,
                "today": True,
                "selected": False,
                "markers": [
                    {"label": "3 citas", "kind": "busy"},
                    {"label": "2 confirmadas", "kind": "neutral"},
                ],
            },
            {
                "number": 8,
                "outside": False,
                "today": False,
                "selected": True,
                "markers": [
                    {"label": "4 citas", "kind": "busy"},
                    {"label": "bloqueo parcial", "kind": "blocked"},
                ],
            },
            {"number": 9, "outside": False, "today": False, "selected": False, "markers": []},
            {
                "number": 10,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "2 citas", "kind": "busy"},
                ],
            },
            {"number": 11, "outside": False, "today": False, "selected": False, "markers": []},
            {"number": 12, "outside": False, "today": False, "selected": False, "markers": []},
        ],
        [
            {"number": 13, "outside": False, "today": False, "selected": False, "markers": []},
            {
                "number": 14,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "4 citas", "kind": "busy"},
                ],
            },
            {
                "number": 15,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "bloqueo parcial", "kind": "blocked"},
                ],
            },
            {"number": 16, "outside": False, "today": False, "selected": False, "markers": []},
            {
                "number": 17,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "2 citas", "kind": "busy"},
                ],
            },
            {"number": 18, "outside": False, "today": False, "selected": False, "markers": []},
            {"number": 19, "outside": False, "today": False, "selected": False, "markers": []},
        ],
        [
            {
                "number": 20,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "2 citas", "kind": "busy"},
                ],
            },
            {
                "number": 21,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "5 citas", "kind": "busy"},
                ],
            },
            {"number": 22, "outside": False, "today": False, "selected": False, "markers": []},
            {"number": 23, "outside": False, "today": False, "selected": False, "markers": []},
            {
                "number": 24,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "2 citas", "kind": "busy"},
                ],
            },
            {"number": 25, "outside": False, "today": False, "selected": False, "markers": []},
            {"number": 26, "outside": False, "today": False, "selected": False, "markers": []},
        ],
        [
            {
                "number": 27,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "2 citas", "kind": "busy"},
                ],
            },
            {"number": 28, "outside": False, "today": False, "selected": False, "markers": []},
            {
                "number": 29,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "4 citas", "kind": "busy"},
                ],
            },
            {
                "number": 30,
                "outside": False,
                "today": False,
                "selected": False,
                "markers": [
                    {"label": "1 bloqueo", "kind": "blocked"},
                ],
            },
            {"number": 1, "outside": True, "today": False, "selected": False, "markers": []},
            {"number": 2, "outside": True, "today": False, "selected": False, "markers": []},
            {"number": 3, "outside": True, "today": False, "selected": False, "markers": []},
        ],
    ],
    "selected_day_title": "Miercoles 8 de abril",
    "selected_day_summary": "4 tramos ocupados, 3 limpios y 1 bloqueo parcial.",
    "agenda_timeline_slots": [
        {
            "time": "09:00",
            "entries": [
                {
                    "name": "Marta Leon",
                    "service": "Fisio inicial",
                    "status": "Confirmada",
                    "status_key": "confirmed",
                },
            ],
            "blocked_label": "",
        },
        {
            "time": "10:00",
            "entries": [
                {
                    "name": "Carlos Ruiz",
                    "service": "Revision",
                    "status": "Completada",
                    "status_key": "completed",
                },
                {
                    "name": "Sofia Marquez",
                    "service": "Control",
                    "status": "Pendiente",
                    "status_key": "pending",
                },
            ],
            "blocked_label": "",
        },
        {
            "time": "11:00",
            "entries": [],
            "blocked_label": "",
        },
        {
            "time": "12:00",
            "entries": [
                {
                    "name": "Ana Perez",
                    "service": "Seguimiento",
                    "status": "Pendiente",
                    "status_key": "pending",
                },
                {
                    "name": "Raul Soto",
                    "service": "Primera",
                    "status": "Cancelada",
                    "status_key": "cancelled",
                },
                {
                    "name": "Nora Vidal",
                    "service": "Llamada",
                    "status": "No asistio",
                    "status_key": "missed",
                },
            ],
            "blocked_label": "",
        },
        {
            "time": "13:00",
            "entries": [],
            "blocked_label": "Bloqueo parcial",
        },
        {
            "time": "16:00",
            "entries": [],
            "blocked_label": "",
        },
        {
            "time": "17:00",
            "entries": [
                {
                    "name": "Lucia Gomez",
                    "service": "Evaluacion",
                    "status": "Confirmada",
                    "status_key": "confirmed",
                },
            ],
            "blocked_label": "",
        },
        {
            "time": "18:00",
            "entries": [],
            "blocked_label": "",
        },
    ],
}


class AppEntryPointView(TemplateView):
    template_name = "core/app_entrypoint.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(AGENDA_PHASE_ONE_CONTEXT)
        return context


class UIValidationView(TemplateView):
    template_name = "core/ui_preview.html"


class CalendarUIValidationView(TemplateView):
    template_name = "core/calendar_ui_preview.html"
