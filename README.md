# Agenda de Citas

App Django server-rendered para gestión de agenda y citas.

## Configuración por entorno

El proyecto usa un único `config/settings.py` parametrizado por variables de entorno.
No carga archivos `.env` automáticamente: en local o en servidor, las variables deben exportarse desde el shell o desde el gestor de procesos correspondiente.

### Variables soportadas

#### Núcleo

- `SECRET_KEY`: clave secreta de Django. En producción debe ser propia y segura.
- `DEBUG`: `True` o `False`. En producción debe ir en `False`.
- `ALLOWED_HOSTS`: lista separada por comas. Ejemplo: `agenda.example.com,www.agenda.example.com`.
- `CSRF_TRUSTED_ORIGINS`: lista separada por comas con URLs completas. Ejemplo: `https://agenda.example.com,https://www.agenda.example.com`.
- `WAGTAILADMIN_BASE_URL`: URL pública base de admin. Ejemplo: `https://agenda.example.com`.
- `TIME_ZONE`: zona horaria operativa. Por defecto queda en `Europe/Madrid`.

#### Base de datos

Si no hay variables PostgreSQL definidas, el proyecto usa SQLite local en `db.sqlite3`.

Para activar PostgreSQL:

- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`
- `POSTGRES_HOST` opcional, por defecto `127.0.0.1`
- `POSTGRES_PORT` opcional, por defecto `5432`
- `POSTGRES_SSLMODE` opcional
- `POSTGRES_CONN_MAX_AGE` opcional, por defecto `60`

#### Flags HTTPS opcionales

- `SECURE_SSL_REDIRECT`
- `SESSION_COOKIE_SECURE`
- `CSRF_COOKIE_SECURE`
- `SECURE_HSTS_SECONDS`
- `SECURE_HSTS_INCLUDE_SUBDOMAINS`
- `SECURE_HSTS_PRELOAD`
- `USE_X_FORWARDED_HOST`
- `USE_X_FORWARDED_PORT`
- `USE_X_FORWARDED_PROTO`

## Arranque local

Con la configuración local por defecto:

```bash
.venv/bin/python manage.py migrate
.venv/bin/python manage.py runserver
```

## Arranque productivo básico del repo

Este repo queda preparado para un despliegue clásico con:

- variables de entorno
- PostgreSQL
- Gunicorn
- `migrate`
- `collectstatic`

Secuencia base del siguiente bloque de despliegue:

```bash
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/gunicorn config.wsgi:application --bind 0.0.0.0:8000
```

La configuración de Nginx, `systemd`, TLS y servidor queda fuera de este microbloque.
