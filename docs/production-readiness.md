# Otzi Expenses: Production Readiness

## Estado actual

- La app es Django 5 con PostgreSQL, Redis, Celery y storage en Azure Blob.
- El `docker-compose` actual solo levanta dependencias locales: PostgreSQL, Redis y Azurite.
- No existe `Dockerfile`, ni servicio web, ni servicio worker, ni proceso de migraciones para producción.
- La configuración de settings mezcla supuestos de desarrollo dentro de `base.py`.
- El repositorio contiene artefactos que no deberían versionarse: `.venv`, `__pycache__`, `staticfiles`, `env/.env.dev`.

## Bloqueos principales para producción

1. No hay build reproducible.
   - Faltan `requirements.txt` o `pyproject.toml`.
   - Faltan `Dockerfile` y comandos de arranque.

2. La configuración base no es segura para producción.
   - `DEBUG = 1` está fijo en `waexp/settings/base.py`.
   - `base.py` siempre carga `env/.env.dev`.
   - `prod.py` construye mal `CSRF_TRUSTED_ORIGINS`.

3. El webhook de WhatsApp no es production-safe.
   - Usa estado en memoria (`user_states`) que se pierde al reiniciar y no funciona bien con múltiples réplicas.
   - Crea gastos con `created_by_id=1`.
   - Hace llamadas HTTP síncronas al recibir el webhook.

4. El repositorio necesita higiene antes de automatizar deploy.
   - Se versionaron secretos y archivos generados.
   - El repo pesa demasiado para builds limpios y lentos.

5. No hay red de seguridad.
   - `manage.py test` corre pero no encuentra tests.

## Arquitectura objetivo recomendada

- `web`: Django + Gunicorn
- `worker`: Celery worker
- `db`: PostgreSQL administrado o contenedor dedicado
- `redis`: Redis para broker/result backend
- Azure Blob Storage real en vez de Azurite
- Proxy reverso externo del servidor o del proveedor

## Orden de trabajo recomendado

### Fase 1: saneamiento del repo

- Agregar `.gitignore` de raíz.
- Sacar del repo `.venv`, `__pycache__`, `*.pyc`, `staticfiles`, archivos de media y secretos.
- Reemplazar `env/.env.dev` por `env/.env.example`.
- Crear `requirements.txt` versionado desde el entorno actual.

### Fase 2: separar dev y prod

- Hacer que `base.py` no fuerce `DEBUG` ni cargue `.env.dev` siempre.
- Mover la carga de `.env.dev` a `dev.py`.
- Endurecer `prod.py` con `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, cookies seguras y configuración detrás de proxy.

### Fase 3: dockerización real

- Crear `Dockerfile`.
- Crear un `docker-compose.prod.yml` o equivalente para:
  - `web`
  - `worker`
  - `redis`
  - `postgres` si no usarás uno administrado
- Definir `entrypoint` para `migrate` y `collectstatic`.

### Fase 4: endurecer el flujo WhatsApp

- Persistir el estado conversacional en base de datos o Redis.
- Eliminar `created_by_id=1`.
- Mover descarga de media y acciones lentas a Celery.
- Agregar logs estructurados y manejo de errores.

### Fase 5: mínimos de verificación

- Tests del webhook.
- Tests de settings de producción.
- Smoke test de login, dashboard y carga de adjuntos.

## Estrategia de ramas

No recomiendo usar una rama permanente `prod` como lugar donde “vive” la producción.

Recomendación:

- `main`: integración estable
- ramas cortas tipo `feature/*` o `hardening/*`
- tags o releases para lo desplegado a producción

Solo usaría una rama `prod` si tu proceso de deploy depende explícitamente de esa rama y no puedes cambiarlo. Si la creas, que sea una rama de release estricta, no una rama larga donde se desarrollan fixes a mano.

## Primer entregable razonable

Antes de pensar en desplegar al servidor, el primer hito debería ser este:

- repo limpio
- dependencias versionadas
- `Dockerfile`
- compose de producción
- settings `dev` y `prod` realmente separados
- reemplazo de Azurite por Azure Blob real vía variables de entorno
