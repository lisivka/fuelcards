#!/usr/bin/env bash
# Оновлення застосунку на сервері. Запуск: sudo bash /opt/fuelcards/deploy/deploy.sh
set -euo pipefail

APP_DIR=/opt/fuelcards
APP_USER=fuelcards
SERVICE=fuelcards

if [[ $EUID -ne 0 ]]; then
    echo "Запустіть через sudo: sudo bash $0" >&2
    exit 1
fi

run() { sudo -u "$APP_USER" "$@"; }

cd "$APP_DIR"
run git pull --ff-only
run "$APP_DIR/venv/bin/pip" install -r requirements.txt
run "$APP_DIR/venv/bin/python" manage.py migrate --noinput
run "$APP_DIR/venv/bin/python" manage.py collectstatic --noinput
systemctl restart "$SERVICE"
sleep 2
systemctl --no-pager --lines=5 status "$SERVICE"