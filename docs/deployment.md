# Despliegue

Guía operativa mínima para un despliegue tradicional de este repo con Django + Gunicorn + PostgreSQL + Nginx.

Este documento no despliega nada por sí mismo.
Solo deja claro qué soporta hoy el repo y qué pasos deben ejecutarse después en servidor.

## Alcance real del repo

El código actual ya deja resuelto dentro del repo:

- `config/settings.py` único por variables de entorno
- fallback local a SQLite y activación de PostgreSQL por `POSTGRES_*`
- `gunicorn` y `psycopg[binary]` en `requirements.txt`
- `config.wsgi:application` como punto de entrada
- `STATIC_ROOT` en `staticfiles/`
- `MEDIA_ROOT` en `media/`
- `manage.py migrate`
- `manage.py collectstatic`

El repo no incluye ni decide todavía:

- configuración final de Nginx
- unidad `systemd`
- TLS / Certbot
- aprovisionamiento real de PostgreSQL
- backups, monitorización o rotación de logs

## Variables de entorno de producción

Base mínima obligatoria:

- `SECRET_KEY`
- `DEBUG=False`
- `ALLOWED_HOSTS`
- `CSRF_TRUSTED_ORIGINS`
- `WAGTAILADMIN_BASE_URL`
- `TIME_ZONE`
- `POSTGRES_DB`
- `POSTGRES_USER`
- `POSTGRES_PASSWORD`

Normalmente también harán falta estas:

- `POSTGRES_HOST`
- `POSTGRES_PORT`
- `POSTGRES_SSLMODE`
- `POSTGRES_CONN_MAX_AGE`
- `SECURE_SSL_REDIRECT`
- `SESSION_COOKIE_SECURE`
- `CSRF_COOKIE_SECURE`
- `SECURE_HSTS_SECONDS`
- `SECURE_HSTS_INCLUDE_SUBDOMAINS`
- `USE_X_FORWARDED_HOST`
- `USE_X_FORWARDED_PORT`
- `USE_X_FORWARDED_PROTO`

Variable opcional con decisión explícita:

- `SECURE_HSTS_PRELOAD`

Notas reales:

- Si falta `SECRET_KEY` con `DEBUG=False`, Django no arranca.
- Si se define cualquier `POSTGRES_*`, el proyecto exige que `POSTGRES_DB`, `POSTGRES_USER` y `POSTGRES_PASSWORD` estén completos.
- Si no hay `POSTGRES_*`, el proyecto cae a SQLite. Eso sirve en local, pero no debe ser la base de producción.
- `.env.example` es solo una referencia. Django no lee `.env` automáticamente en este repo.

## Bootstrap exacto en servidor

Orden recomendado dentro del checkout del repo:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Después, cargar las variables de entorno de producción desde shell, `systemd` o el mecanismo equivalente del servidor.

Con el entorno ya cargado:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py check --deploy
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
```

Si el despliegue necesita acceso administrativo real:

```bash
.venv/bin/python manage.py createsuperuser
```

Arranque base de Gunicorn para validación:

```bash
.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000
```

## PostgreSQL

El repositorio no crea la base de datos ni el usuario.
Eso debe resolverse en el servidor antes de ejecutar `migrate`.

Contrato real del código:

- desarrollo local: SQLite si no hay `POSTGRES_*`
- producción: PostgreSQL

No está documentado en el repo ningún flujo de producción serio sobre SQLite.

## Gunicorn

El repo ya incluye todo lo mínimo para arrancar Gunicorn:

- dependencia en `requirements.txt`
- entrada `config.wsgi:application`

Lo que sigue fuera del repo:

- proceso persistente con `systemd`
- socket o puerto definitivo
- usuario de sistema
- reinicios y logs del servicio

## Estáticos y media

`collectstatic` está soportado por el proyecto y recopila en `staticfiles/`.

`/media/` solo se sirve automáticamente con `DEBUG=True`.
En producción, si se van a usar Wagtail Documents, Wagtail Images o cualquier fichero subido, Nginx u otro servidor frontal debe servir `MEDIA_ROOT`.

## Wagtail y Site

Wagtail sigue instalado y forma parte del arranque del proyecto.

Advertencia real:

- la migración [home/migrations/0002_create_homepage.py](../home/migrations/0002_create_homepage.py) crea el `Site` por defecto con `hostname="localhost"`

Antes de un despliegue real hay que actualizar ese `Site` al hostname público real.
Eso puede hacerse desde el admin de Wagtail o desde shell, pero no queda automatizado en el repo.

Además:

- `WAGTAILADMIN_BASE_URL` debe apuntar a la URL pública real
- `/admin/` sigue siendo el admin de Wagtail
- `/django-admin/` sigue exponiendo el admin nativo de Django

## Demo pública

El repo ya deja preparada la pieza local para reiniciar la demo pública cada día, pero no automatiza todavía su ejecución en servidor.

Hechos reales del código:

- [config/settings.py](../config/settings.py) fija el usuario demo oficial y la contraseña demo oficial como contrato estable
- [core/management/commands/reset_agenda_demo.py](../core/management/commands/reset_agenda_demo.py) restablece la demo a un estado base repetible y mantiene esas credenciales fijas
- [core/management/commands/seed_agenda_demo.py](../core/management/commands/seed_agenda_demo.py) reutiliza la misma lógica y queda como compatibilidad
- el reset recrea datos operativos demo de agenda, clientes, servicios, bloqueos, cierres y citas sin dejar duplicados tras ejecuciones repetidas
- el repo sigue sin incluir cron, timer ni automatización remota de esa ejecución diaria

Conclusión operativa:

- el comando que debe invocarse diariamente en producción es `.venv/bin/python manage.py reset_agenda_demo`
- ese comando garantiza que el usuario demo oficial existe, que su contraseña vuelve a fijarse al valor oficial vigente y que los datos demo vuelven a su base reproducible
- las credenciales demo no cambian con el reset diario
- el siguiente bloque pendiente fuera del repo es solo automatizar esa invocación en el servidor de despliegue

## Qué queda fuera del repo y debe resolverse en servidor

- crear usuario de sistema y directorio final de la app
- clonar o copiar el repo al servidor
- preparar PostgreSQL real
- cargar variables de entorno
- definir unidad `systemd` para Gunicorn
- definir Nginx como reverse proxy
- servir `staticfiles/`
- servir `media/` si aplica
- emitir y renovar TLS con Certbot u otra solución

## Validación útil antes de desplegar

Con variables de producción razonables, el repo ya permite validar:

```bash
.venv/bin/python manage.py check
.venv/bin/python manage.py check --deploy
```

En el estado actual del proyecto, `check --deploy` puede seguir mostrando el warning `security.W021` si `SECURE_HSTS_PRELOAD=False`.
Eso no bloquea un primer despliegue técnico, pero sí deja claro que la entrada en preload HSTS no está asumida todavía.
