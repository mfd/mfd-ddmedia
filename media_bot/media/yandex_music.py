"""Authenticated downloads from the original Yandex Music track source."""

from __future__ import annotations

import os
import ipaddress
import re
import shutil
import socket
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests


YANDEX_HOSTS = frozenset({"music.yandex.ru", "music.yandex.com",
                         "www.music.yandex.ru", "www.music.yandex.com"})


def is_yandex_music_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in YANDEX_HOSTS


def track_reference(url: str) -> tuple[str, str | None]:
    """Return a track ID and optional album ID; reject collections and arbitrary paths."""
    path = urlparse(url).path.rstrip("/")
    match = re.fullmatch(r"/track/(\d+)", path)
    if match:
        return match.group(1), None
    match = re.fullmatch(r"/album/(\d+)/track/(\d+)", path)
    if match:
        return match.group(2), match.group(1)
    raise ValueError("Поддерживаются ссылки Яндекс Музыки на отдельный трек.")


def _track(url: str):
    from yandex_music import Client

    token = os.environ.get("YANDEX_MUSIC_TOKEN", "").strip()
    if not token:
        raise ValueError("Яндекс Музыка пока не подключена: нужен токен аккаунта с подпиской Плюс.")
    track_id, album_id = track_reference(url)
    client = Client(token).init()
    reference = f"{track_id}:{album_id}" if album_id else track_id
    tracks = client.tracks([reference])
    if not tracks:
        raise ValueError("Яндекс Музыка не нашла этот трек для подключённого аккаунта.")
    return tracks[0]


def _full_mp3_variants(track) -> list:
    available = track.get_download_info() or []
    variants = [item for item in available
                if item.codec == "mp3" and not getattr(item, "preview", False)
                and isinstance(item.bitrate_in_kbps, int)]
    if not variants:
        if any(not getattr(item, "preview", False) for item in available):
            raise ValueError("Для этого трека нет доступного полного варианта MP3.")
        raise ValueError("Аккаунту доступен только ознакомительный фрагмент этого трека.")
    return variants


def _download_stream(url: str, target: Path, max_bytes: int, min_free_bytes: int,
                     progress) -> None:
    """Write a signed audio response in bounded chunks; never buffer the track in RAM."""
    with requests.Session() as session:
        for _ in range(4):
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise ValueError("Яндекс Музыка вернула небезопасную ссылку на файл.")
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
            if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise ValueError("Яндекс Музыка вернула недопустимый адрес файла.")
            with session.get(url, stream=True, allow_redirects=False, timeout=(10, 30)) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("Яндекс Музыка не отдала файл.")
                    url = urljoin(url, location)
                    continue
                response.raise_for_status()
                total = response.headers.get("Content-Length")
                expected = int(total) if total and total.isdigit() else None
                if expected and expected > max_bytes:
                    raise ValueError("Аудиофайл превысил допустимый размер задачи.")
                received = 0
                with target.open("wb") as stream:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        received += len(chunk)
                        if received > max_bytes:
                            raise ValueError("Аудиофайл превысил допустимый размер задачи.")
                        if shutil.disk_usage(target.parent).free < min_free_bytes:
                            raise ValueError("Недостаточно свободного места на сервере.")
                        stream.write(chunk)
                        progress(received, expected)
                if expected and received != expected:
                    raise ValueError("Яндекс Музыка передала файл не полностью.")
                return
    raise ValueError("Слишком много перенаправлений при загрузке из Яндекс Музыки.")


def metadata(url: str) -> dict:
    track = _track(url)
    bitrate = max(item.bitrate_in_kbps for item in _full_mp3_variants(track))
    duration = (track.duration_ms or 0) / 1000 or None
    artists = ", ".join(artist.name for artist in track.artists if getattr(artist, "name", None))
    cover = track.get_cover_url("400x400") if track.cover_uri else ""
    formats = [{"ext": "mp3", "vcodec": "none", "acodec": "mp3", "abr": bitrate,
                "size": int(duration * bitrate * 1000 / 8) if duration else None}]
    return {"id": str(track.id), "url": url, "title": str(track.title or "Трек")[:300],
            "channel": artists[:200], "channel_url": "", "webpage_url": url,
            "duration": duration, "thumbnail": cover, "upload_date": None,
            "view_count": None, "like_count": None, "comment_count": None,
            "channel_follower_count": None, "description": "", "formats": formats,
            "subtitles": []}


def download(url: str, selection: str, directory: Path, max_bytes: int,
             min_free_bytes: int, max_temp_bytes: int, progress=lambda _amount, _total: None) -> Path:
    if selection not in {"audio:mp3", "audio:m4a"}:
        raise ValueError("У Яндекс Музыки доступна только аудиодорожка.")
    track = _track(url)
    variants = _full_mp3_variants(track)
    variant = max(variants, key=lambda item: item.bitrate_in_kbps)
    if shutil.disk_usage(directory).free < min_free_bytes + 100_000_000:
        raise ValueError("Недостаточно свободного места на сервере.")
    source = directory / "yandex-source.mp3"
    _download_stream(variant.get_direct_link(timeout=30), source,
                     min(max_bytes, max_temp_bytes), min_free_bytes, progress)
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError("Яндекс Музыка не отдала аудиофайл.")
    if source.stat().st_size > min(max_bytes, max_temp_bytes):
        source.unlink(missing_ok=True)
        raise ValueError("Аудиофайл превысил допустимый размер задачи.")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
                           capture_output=True, text=True, timeout=30, check=True)
    actual_duration = float(probe.stdout.strip())
    expected_duration = (track.duration_ms or 0) / 1000
    if expected_duration > 45 and actual_duration < expected_duration * 0.8:
        source.unlink(missing_ok=True)
        raise ValueError("Яндекс Музыка вернула только фрагмент вместо полного трека.")
    if selection == "audio:mp3":
        return source
    target = directory / "yandex-audio.m4a"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                    "-vn", "-threads", "1", "-c:a", "aac", "-b:a", "192k", str(target)],
                   capture_output=True, timeout=3600, check=True)
    source.unlink()
    if not target.is_file() or target.stat().st_size > max_bytes:
        target.unlink(missing_ok=True)
        raise ValueError("Готовый аудиофайл превысил допустимый размер задачи.")
    return target
