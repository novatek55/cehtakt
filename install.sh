#!/bin/sh
set -eu
cd "$(dirname "$0")"
umask 077

command -v openssl >/dev/null || { echo "Установите openssl."; exit 1; }
docker compose version >/dev/null || { echo "Установите Docker Engine и Docker Compose."; exit 1; }
docker info >/dev/null || { echo "Запустите Docker и проверьте доступ к нему."; exit 1; }

if [ ! -f .env ]; then
    database_password=$(openssl rand -hex 32)
    secret_key=$(openssl rand -hex 48)
    (set -C; printf '%s\n' "CEHTAKT_DB_PASSWORD=$database_password" "CEHTAKT_SECRET_KEY=$secret_key" "CEHTAKT_PORT=8080" > .env)
fi

docker compose up -d --build --wait --wait-timeout 240
admin_exists=$(docker compose exec -T app python manage.py shell -v 0 -c \
    'from django.contrib.auth import get_user_model; print(int(get_user_model().objects.filter(is_superuser=True, is_active=True).exists()))')
if [ "$admin_exists" = 0 ]; then
    echo "Создайте администратора «Цехтакта»."
    docker compose exec app python manage.py createsuperuser
fi
port=${CEHTAKT_PORT:-$(sed -n 's/^CEHTAKT_PORT=//p' .env)}
echo "Откройте http://localhost:${port:-8080}/"
echo "Поддержка «Цехтакта» в Telegram: https://t.me/ProMelnik"
