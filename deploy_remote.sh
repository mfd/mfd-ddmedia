#!/usr/bin/env bash
set -euo pipefail

version="${1:?Specify the release version}"
if [[ ! "$version" =~ ^[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]{2}\.[0-9]{2}$ ]]; then
  echo "Invalid release version" >&2
  exit 1
fi

base=/home/aiagent
app="$base/media-bot-app"
stage="$base/media-bot-stage-$version"
backup="$base/media-bot-backup-$(date -u +%Y%m%dT%H%M%SZ)"
archive="$base/media-bot-release-$version.tar.gz"
python="$base/media-bot-venv/bin/python"
env_file="$base/.config/youtube-bot/secrets.env"

test -f "$archive"
test -f "$env_file"
rm -rf "$stage"
mkdir -p "$stage"
tar -xzf "$archive" -C "$stage"
"$python" -m pip install -q -r "$stage/requirements.txt"
"$python" -m compileall -q "$stage"
test_data="$(mktemp -d)"
trap 'rm -rf "$test_data"' EXIT
(cd "$stage" && BOT_TOKEN='123:TEST' OWNER_ID=1 DATA_DIR="$test_data" \
  "$python" -m unittest discover -s tests -q)
grep -Fq "SCRIPT_VERSION = \"$version\"" "$stage/media_bot/app.py"

BOT_ENV_FILE="$env_file" python3 "$stage/notify.py" \
  'В боте ведутся технические работы. Скачивание временно приостановлено; сообщу, когда бот снова заработает.'

mv "$app" "$backup"
if ! mv "$stage" "$app"; then
  mv "$backup" "$app"
  exit 1
fi
rollback() {
  echo "Release failed; restoring $backup" >&2
  mv "$app" "$base/media-bot-failed-$version-$(date -u +%s)"
  mv "$backup" "$app"
  sudo systemctl restart media-bot.service
  BOT_ENV_FILE="$env_file" python3 "$app/notify.py" \
    'Обновление не удалось. Бот возвращён к предыдущей версии.' || true
  exit 1
}

restart_count_before="$(systemctl show media-bot.service -p NRestarts --value)"
sudo systemctl restart media-bot.service || rollback
sleep 10
restart_count_after="$(systemctl show media-bot.service -p NRestarts --value)"
if ! systemctl is-active --quiet media-bot.service || (( restart_count_after > restart_count_before )); then
  rollback
fi

BOT_ENV_FILE="$env_file" python3 "$app/notify.py" --show-changes \
  'Технические работы завершены. Бот снова принимает ссылки.'
echo "Deployed $version; previous version is saved at $backup"
systemctl show media-bot.service -p ActiveState -p ActiveEnterTimestamp -p ExecMainPID
