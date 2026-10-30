#!/usr/bin/env bash
set -euo pipefail

export BOT_ENV_FILE="${1:?Pass the private env file path as the first argument}"
cd "$(dirname "$0")"
"${BOT_PYTHON:?Set BOT_PYTHON in the environment}" -m compileall -q .
python3 notify.py '🔧 В боте ведутся технические работы. Скачивание временно приостановлено; сообщу, когда бот снова заработает.'
restart_count_before="$(systemctl show media-bot.service -p NRestarts --value)"
if ! sudo systemctl restart media-bot.service; then
  python3 notify.py '⚠️ Обновление бота не завершилось. Сервис проверяется.' || true
  exit 1
fi
sleep 10
restart_count_after="$(systemctl show media-bot.service -p NRestarts --value)"
if ! systemctl is-active --quiet media-bot.service || (( restart_count_after > restart_count_before )); then
  python3 notify.py '⚠️ Бот не прошёл проверку после обновления. Сервис проверяется.' || true
  exit 1
fi
python3 notify.py '✅ Технические работы завершены. Бот снова принимает ссылки.'
