#!/bin/sh
set -eu

if [ "${WAIT_FOR_DB:-1}" = "1" ] && [ -n "${POSTGRES_HOST:-}" ]; then
  echo "Waiting for PostgreSQL at ${POSTGRES_HOST}:${POSTGRES_PORT:-5432}..."
  until python -c "import os, socket; host = os.environ['POSTGRES_HOST']; port = int(os.environ.get('POSTGRES_PORT', '5432')); sock = socket.create_connection((host, port), timeout=1); sock.close()" >/dev/null 2>&1; do
    sleep 1
  done
fi

if [ "${RUN_MIGRATIONS:-1}" = "1" ]; then
  python manage.py migrate --noinput
fi

if [ "${COLLECTSTATIC:-1}" = "1" ]; then
  python manage.py collectstatic --noinput
fi

exec "$@"

