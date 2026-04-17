# Agenda de Citas

Agenda de Citas es una app Django server-rendered para negocios que trabajan por cita.
Está planteada como un producto pequeño, claro y defendible para contextos como peluquería, fisioterapia, psicología o servicios similares.

## Enfoque del proyecto

La app no busca resolver "todo" con complejidad innecesaria.
El núcleo del producto es la agenda diaria y mensual, con una operativa sobria:

- calendario mensual a la izquierda y panel diario a la derecha
- creación y edición de citas dentro del flujo principal
- clientes activos y archivados
- servicios configurables
- reglas globales de agenda
- cierres manuales por día o rango
- festivos oficiales sincronizados desde BOE
- acceso autenticado con pantalla propia de login

La implementación sigue una dirección deliberadamente conservadora:

- Django como fuente de verdad
- server-rendered HTML
- `htmx` solo donde aporta valor claro
- sin SPA ni frontend separado

## Estado actual

El repositorio ya contiene una base funcional real del producto:

- agenda principal operativa en `/app/`
- alta y edición de citas
- ficha y gestión básica de clientes
- ajustes de negocio, agenda y servicios
- autenticación orientada a la app
- test suite y validaciones básicas de Django en verde

En términos técnicos, el proyecto ya está preparado para avanzar hacia un despliegue tradicional con Django + Gunicorn + PostgreSQL + Nginx, aunque la ejecución real en servidor se documenta aparte.

## Stack

- Python
- Django
- Wagtail
- SQLite en local
- PostgreSQL para producción
- Gunicorn
- HTML server-rendered
- CSS propio
- `htmx` mínimo

## Desarrollo local

Arranque local mínimo:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

Si no se define ningún `POSTGRES_*`, el proyecto usa SQLite local en `db.sqlite3`.

La referencia base de variables está en `.env.example`.

## Despliegue

La guía técnica de despliegue vive en [docs/deployment.md](docs/deployment.md).

Ahí queda documentado el bloque operativo:

- variables de entorno de producción
- orden exacto de bootstrap
- notas sobre PostgreSQL, Gunicorn y Wagtail
- pasos manuales que quedan fuera del repo
- advertencias actuales sobre demo pública
