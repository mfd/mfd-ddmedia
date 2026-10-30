"""Authorize Yandex Music interactively and store its token outside the app tree."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

ENV_FILE = Path.home() / ".config" / "youtube-bot" / "secrets.env"


def ensure_env_file(path: Path) -> None:
    """Create the private environment file when needed and reject unsafe paths."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(descriptor)
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid():
        raise PermissionError("Файл настроек должен быть обычным файлом текущего пользователя.")
    if stat.S_IMODE(status.st_mode) & 0o077:
        raise PermissionError("Ограничьте доступ к файлу настроек до 600 перед записью токена.")


def save_token(path: Path, token: str) -> None:
    if not token or any(character.isspace() for character in token):
        raise ValueError("Яндекс вернул токен в неожиданном формате.")
    ensure_env_file(path)
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_uid != os.getuid():
        raise PermissionError("Файл настроек должен быть обычным файлом текущего пользователя.")
    if stat.S_IMODE(status.st_mode) & 0o077:
        raise PermissionError("Ограничьте доступ к файлу настроек до 600 перед записью токена.")
    lines = [line for line in path.read_text().splitlines()
             if not line.startswith("YANDEX_MUSIC_TOKEN=")]
    lines.append(f"YANDEX_MUSIC_TOKEN={token}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=".secrets-", delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def authenticate(on_code) -> str:
    """Run Yandex device authorization, validate it, then store its token."""
    from yandex_music import Client

    ensure_env_file(ENV_FILE)
    credentials = Client().device_auth(on_code=on_code)
    token = credentials.access_token
    client = Client(token).init()
    if not client.me:
        raise RuntimeError("Яндекс не подтвердил авторизацию.")
    save_token(ENV_FILE, token)
    return token


def main() -> None:
    def on_code(code) -> None:
        print(f"Откройте {code.verification_url} и введите код: {code.user_code}")

    authenticate(on_code)
    print("Аккаунт Яндекс Музыки подключён. Токен сохранён в закрытом файле настроек.")
    print("Перезапустите службу: sudo systemctl restart media-bot.service")


if __name__ == "__main__":
    main()
