#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Запустите установщик от root: sudo ./install.sh"
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="eucalrent"

apt-get update
apt-get install -y python3 python3-venv python3-pip git ca-certificates build-essential

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip wheel setuptools
"${APP_DIR}/.venv/bin/pip" install -r "${APP_DIR}/requirements.txt"
"${APP_DIR}/.venv/bin/pip" install -e "${APP_DIR}" --no-deps

mkdir -p "${APP_DIR}/data"
chmod 700 "${APP_DIR}/data"

if [[ ! -f "${APP_DIR}/.env" ]]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
fi
chmod 600 "${APP_DIR}/.env"

sed \
  -e "s|@@APP_DIR@@|${APP_DIR}|g" \
  "${APP_DIR}/deploy/eucalrent.service" \
  > "/etc/systemd/system/${SERVICE_NAME}.service"

systemctl daemon-reload

echo
echo "Установка завершена."
echo "1. Заполните ${APP_DIR}/.env"
echo "2. Проверьте: ${APP_DIR}/.venv/bin/python -m kosell_bot --check"
echo "3. Запустите: systemctl enable --now ${SERVICE_NAME}"
