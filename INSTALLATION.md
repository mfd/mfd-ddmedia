# Installation and operations

## English

### Requirements

Use Linux with Python 3.12+, FFmpeg and ffprobe, a Telegram bot token, and enough disk space for temporary media. Install Python packages from `requirements.txt` in a virtual environment. The bot uses SQLite and stores downloaded media under `DATA_DIR`. It serves temporary links over HTTPS; provide a certificate and key for the public hostname.

### Configuration

Copy `config.example.env` to a private environment file outside the repository and fill its variables. Set `BOT_TOKEN`, `OWNER_ID`, `DATA_DIR`, `LINK_BIND_HOST`, `LINK_BIND_PORT`, `PUBLIC_BASE_URL`, `TLS_CERT_FILE`, and `TLS_KEY_FILE`. Set `LINK_BIND_PORT=8080` and `PUBLIC_BASE_URL=https://your-host:8080` if using the requested public port. `LEGACY_LINK_PORT=8443` can keep already issued links alive during migration. Restrict the environment file to the bot account; never commit it.

For files over Telegram's cloud Bot API upload limit, run the local Telegram Bot API server and set `LOCAL_API_URL` to its local endpoint. The local server needs its own Telegram API ID and hash. Keep it reachable only from the bot host. Run one bot instance against one SQLite database and one polling token.

### Run and deploy

Run `python main.py` from this directory with the configured environment loaded. For production, use a systemd service with this directory as its working directory, the private environment file, and the virtual environment's Python executable. Grant the service write access to `DATA_DIR`. After updating the code, run `deploy.sh` to announce maintenance, restart the service and verify it returned to active state. Review service status with `systemctl status media-bot.service` and the journal with `journalctl -u media-bot.service`.

The bot stores job history in `DATA_DIR/jobs.sqlite3`, media in `DATA_DIR/jobs/`, and private error logs in `DATA_DIR/logs/`. Error logs are automatically pruned after 48 hours. Public file links expire after two hours; history is retained for 30 days. The bot pauses new downloads if free disk space falls below 100 MB.

## Русский

### Требования

Нужны Linux, Python 3.12+, FFmpeg и ffprobe, токен Telegram-бота и место на диске для временных файлов. Установите зависимости из `requirements.txt` в виртуальное окружение. Бот хранит историю в SQLite и файлы в `DATA_DIR`. Для временных ссылок нужен HTTPS-сертификат публичного домена.

### Настройка

Скопируйте `config.example.env` в закрытый файл вне репозитория и заполните переменные. Укажите `BOT_TOKEN`, `OWNER_ID`, `DATA_DIR`, `LINK_BIND_HOST`, `LINK_BIND_PORT`, `PUBLIC_BASE_URL`, `TLS_CERT_FILE` и `TLS_KEY_FILE`. Для публичного порта 8080 используйте `LINK_BIND_PORT=8080` и `PUBLIC_BASE_URL=https://ваш-домен:8080`. `LEGACY_LINK_PORT=8443` сохраняет работу ранее выданных ссылок на время перехода. Ограничьте доступ к файлу окружения и не публикуйте его.

Для отправки файлов больше ограничения облачного Bot API запустите локальный сервер Telegram Bot API и задайте `LOCAL_API_URL`. Локальному серверу отдельно нужны Telegram API ID и hash. Не открывайте его наружу. С одним токеном и одной базой SQLite должен работать только один экземпляр бота.

### Запуск и обновление

Запускайте `python main.py` из этой папки с загруженными переменными окружения. В production настройте службу systemd: эта папка как рабочая директория, закрытый env-файл и Python из виртуального окружения. Службе нужен доступ на запись в `DATA_DIR`. После обновления кода `deploy.sh` объявляет технические работы, перезапускает службу и проверяет, что она активна. Состояние смотрите через `systemctl status media-bot.service`, журнал службы — через `journalctl -u media-bot.service`.

История задач находится в `DATA_DIR/jobs.sqlite3`, медиа — в `DATA_DIR/jobs/`, закрытые журналы ошибок — в `DATA_DIR/logs/`. Записи ошибок старше 48 часов удаляются автоматически. Публичные ссылки действуют два часа, история хранится 30 дней. При остатке диска менее 100 МБ новые загрузки приостанавливаются.
