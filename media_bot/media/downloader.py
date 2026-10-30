from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import yt_dlp
from yt_dlp.utils import DownloadError, download_range_func

from .kinescope import public_source
from . import yandex_music


class MediaError(Exception):
    def __init__(self, message: str, diagnostic: str = "", code: str | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _probe_url_allowed(media_url: str) -> bool:
    parsed = urlparse(media_url)
    host = (parsed.hostname or "").lower()
    suffix = os.environ.get("MEDIA_SIZE_PROBE_HOST_SUFFIX", "").lower().lstrip(".")
    return bool(parsed.scheme == "https" and parsed.port in (None, 443)
                and not parsed.username and not parsed.password and suffix
                and (host == suffix or host.endswith("." + suffix)))


def _instagram_size(fmt: dict[str, Any]) -> int | None:
    media_url = fmt.get("url") or ""
    if not _probe_url_allowed(media_url):
        return None
    request = urllib.request.Request(media_url, method="HEAD", headers=fmt.get("http_headers") or {})
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=8) as response:
            size = int(response.headers.get("Content-Length") or 0)
            return size if 0 < size < 4 * 1024**3 else None
    except (OSError, ValueError):
        return None


def normalize_media_url(raw: str) -> str:
    parsed = urlparse(raw.strip())
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise MediaError("У ссылки неверный порт.") from exc
    if (parsed.scheme != "https" or not host or parsed.username or parsed.password
            or port not in (None, 443) or len(raw) > 2048):
        raise MediaError("Пришлите ссылку HTTPS на публикацию с медиа.")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid")):
        raise MediaError("Локальные адреса не поддерживаются.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise MediaError("Локальные адреса не поддерживаются.")
    return raw.strip()


def _source_for_extraction(url: str) -> tuple[str, dict[str, str]]:
    host = (urlparse(url).hostname or "").lower()
    if host not in {"kinescope.io", "www.kinescope.io"}:
        return url, {}
    try:
        return public_source(url)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise MediaError("Kinescope не разрешил доступ к этому видео.") from exc
        raise MediaError("Не удалось открыть страницу Kinescope.") from exc
    except (OSError, ValueError) as exc:
        raise MediaError("Не удалось получить доступный видеопоток Kinescope.") from exc


def extract_metadata(url: str) -> dict[str, Any]:
    host = urlparse(url).hostname
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise MediaError("Не удалось найти сервер по этой ссылке.") from exc
    if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise MediaError("Ссылка ведёт на локальный адрес.")
    if yandex_music.is_yandex_music_url(url):
        try:
            return yandex_music.metadata(url)
        except ValueError as exc:
            raise MediaError(str(exc), diagnostic=type(exc).__name__) from exc
        except Exception as exc:
            raise MediaError("Яндекс Музыка не отдала данные для этой ссылки.",
                             diagnostic=type(exc).__name__) from exc
    source_url, page_meta = _source_for_extraction(url)
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
            "socket_timeout": 15, "retries": 1, "extractor_retries": 1}
    if source_url != url:
        opts["http_headers"] = {"Referer": url}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(source_url, download=False)
        except DownloadError as exc:
            message = str(exc).lower()
            if (urlparse(url).hostname or "").lower() in {"music.yandex.ru", "music.yandex.com"}:
                raise MediaError(
                    "Яндекс Музыка не отдала данные для этой ссылки. Попробуйте позже.",
                    diagnostic=f"{type(exc).__name__}: {exc}",
                ) from exc
            if "youtube" in (urlparse(url).hostname or "") and any(
                    marker in message for marker in (
                        "video is unavailable", "video unavailable", "private video",
                        "not available in your country", "sign in to confirm your age")):
                raise MediaError(
                    "YouTube сообщает, что видео недоступно для загрузчика. Оно может быть удалено, "
                    "приватным или ограниченным по возрасту/региону; Shorts-ссылки поддерживаются, "
                    "но такой ролик нельзя скачать без доступа к нему.") from exc
            raise MediaError("YouTube или источник не отдал данные. Проверьте, что ролик доступен без входа.") from exc
    if isinstance(info, dict) and info.get("_type") == "playlist":
        entries = [entry for entry in info.get("entries") or [] if entry]
        if len(entries) == 1:
            info = entries[0]
        elif len(entries) > 1:
            raise MediaError("Эта ссылка содержит несколько медиафайлов. Обработка подборок и каруселей ещё добавляется.")
    if not isinstance(info, dict) or not info.get("id"):
        raise MediaError("Не удалось получить данные о медиафайле по этой ссылке.")
    is_instagram = info.get("extractor_key") == "Instagram"
    duration = info.get("duration")
    if is_instagram and not duration:
        duration = next((fmt.get("duration") for fmt in info.get("formats") or []
                         if isinstance(fmt.get("duration"), (int, float))), None)
    formats = []
    for fmt in (info.get("formats") or [])[-300:]:
        formats.append({
            "width": fmt.get("width"), "height": fmt.get("height"), "ext": fmt.get("ext"),
            "vcodec": fmt.get("vcodec"), "acodec": fmt.get("acodec"),
            "size": fmt.get("filesize") or fmt.get("filesize_approx") or (_instagram_size(fmt) if is_instagram else None),
            "tbr": fmt.get("tbr"), "abr": fmt.get("abr"),
        })
    title = str(page_meta.get("og:title") or info.get("title") or "Медиа")[:300]
    channel = str(info.get("channel") or info.get("uploader") or "")[:200]
    if is_instagram and title.startswith("Video by ") and info.get("uploader"):
        title = str(info["uploader"])
        channel = ""
    result = {
        "id": urlparse(url).path.rstrip("/").split("/")[-1] if source_url != url else str(info["id"]),
        "url": url, "title": title,
        "channel": channel,
        "channel_url": str(info.get("channel_url") or info.get("uploader_url") or "")[:2048],
        "webpage_url": url if source_url != url else str(info.get("webpage_url") or url)[:2048],
        "duration": duration, "thumbnail": str(page_meta.get("og:image") or info.get("thumbnail") or "")[:2048],
        "upload_date": info.get("upload_date"), "view_count": info.get("view_count"),
        "like_count": info.get("like_count"), "comment_count": info.get("comment_count"),
        "channel_follower_count": info.get("channel_follower_count"),
        "description": str(page_meta.get("og:description") or info.get("description") or "")[:20000],
        "formats": formats,
        "subtitles": sorted(set((info.get("subtitles") or {}) | (info.get("automatic_captions") or {})))[:100],
    }
    return result


def is_portrait(info: dict[str, Any]) -> bool:
    video = [fmt for fmt in info.get("formats", []) if isinstance(fmt.get("width"), int)
             and isinstance(fmt.get("height"), int) and fmt.get("vcodec") not in (None, "none")]
    if not video:
        return False
    largest = max(video, key=lambda fmt: fmt["width"] * fmt["height"])
    return largest["height"] > largest["width"]


def offered_heights(info: dict[str, Any]) -> list[int]:
    dimension = "width" if is_portrait(info) else "height"
    heights = {fmt[dimension] for fmt in info.get("formats", []) if isinstance(fmt.get(dimension), int)
               and fmt.get("vcodec") not in (None, "none")}
    return [height for height in (2160, 1440, 1080, 720, 576, 540, 480, 360, 240, 144) if height in heights]


def _estimated_bytes(fmt: dict[str, Any], duration: int | float | None) -> int | None:
    size = fmt.get("size")
    if isinstance(size, (int, float)) and size > 0:
        return int(size)
    bitrate = fmt.get("tbr") or fmt.get("abr")
    if isinstance(bitrate, (int, float)) and bitrate > 0 and duration:
        return int(bitrate * 1000 * duration / 8)
    return None


def estimated_size(info: dict[str, Any], selection: str) -> int | None:
    """Estimate the selected output, including the audio stream for video-only formats."""
    formats = info.get("formats") or []
    duration = info.get("duration")
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    if selection == "audio:mp3":
        return int(duration * 192_000 / 8) if duration else None
    if selection == "audio:m4a":
        choices = [f for f in audio if f.get("ext") == "m4a"] or audio
        chosen = max(choices, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
        return _estimated_bytes(chosen, duration) if chosen else None
    if not selection.startswith("video:"):
        return None
    target = selection.split(":")[1]
    height = int(target) if target != "best" else None
    dimension = "width" if selection.endswith(":portrait") else "height"
    video = [f for f in formats if f.get("vcodec") not in (None, "none") and
             (height is None or isinstance(f.get(dimension), int) and f[dimension] <= height)]
    exact = [f for f in video if height is None or f.get(dimension) == height]
    preferred = [f for f in exact if f.get("ext") == "mp4" and str(f.get("vcodec") or "").startswith("avc1")]
    video = preferred or exact or video
    split = [f for f in video if f.get("acodec") == "none"]
    split_choices = [f for f in split if f.get("ext") == "mp4"] or split
    # A measured stream size is more useful than a high bitrate estimate for
    # another representation of the same resolution.
    known_size_choices = [f for f in split_choices if isinstance(f.get("size"), (int, float)) and f["size"] > 0]
    chosen_video = max(known_size_choices or split_choices,
                       key=lambda f: (f.get(dimension) or 0, f.get("tbr") or 0), default=None)
    if chosen_video:
        chosen_audio = max([f for f in audio if f.get("ext") == "m4a"] or audio,
                           key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
        video_bytes = _estimated_bytes(chosen_video, duration)
        audio_bytes = _estimated_bytes(chosen_audio, duration) if chosen_audio else None
        if video_bytes and audio_bytes:
            return video_bytes + audio_bytes
        # HLS manifests may expose a variant bitrate but no separate audio bitrate.
        # The variant estimate is still useful; the UI marks it as approximate.
        return video_bytes
    progressive = [f for f in video if f.get("acodec") not in (None, "none")]
    chosen = max(progressive, key=lambda f: (f.get(dimension) or 0, f.get("tbr") or 0), default=None)
    return _estimated_bytes(chosen, duration) if chosen else None


def format_selector(selection: str) -> str:
    if selection == "video:best":
        return "bestvideo[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/best[vcodec^=avc1][ext=mp4]/bestvideo+bestaudio/best"
    if selection.startswith("video:"):
        height = int(selection.split(":")[1])
        if height not in (144, 240, 360, 480, 540, 576, 720, 1080, 1440, 2160):
            raise MediaError("Неверное разрешение.")
        dimension = "width" if selection.endswith(":portrait") else "height"
        return (f"bestvideo[{dimension}={height}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
                f"best[{dimension}={height}][vcodec^=avc1][ext=mp4]/"
                f"bestvideo[{dimension}={height}]+bestaudio/best[{dimension}={height}]/"
                f"bestvideo[{dimension}<={height}][vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
                f"bestvideo[{dimension}<={height}]+bestaudio/best[{dimension}<={height}]")
    if selection == "audio:m4a":
        return "bestaudio[ext=m4a]/bestaudio"
    if selection == "audio:mp3":
        return "bestaudio"
    raise MediaError("Неверный формат.")


def download_media(url: str, selection: str, job_dir: Path, max_bytes: int,
                   min_free_bytes: int, max_temp_bytes: int,
                   progress: Callable[[int, int | None], None], cancelled: Callable[[], bool],
                   apple_compatible: bool = True,
                   clip_range: tuple[int, int] | None = None) -> Path:
    job_dir.mkdir(parents=True, exist_ok=True)
    if yandex_music.is_yandex_music_url(url):
        try:
            return yandex_music.download(url, selection, job_dir, max_bytes,
                                         min_free_bytes, max_temp_bytes, progress)
        except ValueError as exc:
            raise MediaError(str(exc), diagnostic=type(exc).__name__) from exc
        except Exception as exc:
            raise MediaError("Не удалось получить полный трек из Яндекс Музыки.",
                             diagnostic=type(exc).__name__) from exc
    selector = format_selector(selection)
    last_disk_check = 0.0

    def hook(data: dict[str, Any]) -> None:
        nonlocal last_disk_check
        if cancelled():
            raise MediaError("Загрузка отменена.")
        now = time.monotonic()
        if now - last_disk_check >= 1:
            last_disk_check = now
            if shutil.disk_usage(job_dir).free < min_free_bytes:
                raise MediaError("Недостаточно свободного места на сервере.")
            if sum(path.lstat().st_size for path in job_dir.rglob("*")
                   if path.is_file() and not path.is_symlink()) > max_temp_bytes:
                raise MediaError("Временные файлы превысили лимит задачи.")
        amount = int(data.get("downloaded_bytes") or 0)
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        if amount > max_bytes:
            raise MediaError(
                f"Фактический размер превысил доступный лимит {max_bytes / 1024**2:.0f} МБ. "
                "Размер в карточке был приблизительным. Освободите место или выберите качество ниже."
            )
        progress(amount, int(total) if total else None)

    opts: dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "format": selector,
        "paths": {"home": str(job_dir)}, "outtmpl": "media.%(ext)s", "merge_output_format": "mp4",
        "progress_hooks": [hook], "socket_timeout": 30, "retries": 3, "fragment_retries": 3,
        "overwrites": True,
    }
    if clip_range:
        opts["download_ranges"] = download_range_func(None, [clip_range])
        opts["force_keyframes_at_cuts"] = True
    source_url, _ = _source_for_extraction(url)
    if source_url != url:
        opts["http_headers"] = {"Referer": url}
    if selection == "audio:mp3":
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(source_url, download=True)
    candidates = [p for p in job_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not candidates:
        raise MediaError("Файл не появился после скачивания.")
    final = max(candidates, key=lambda p: p.stat().st_size)
    if selection.startswith("video:") and apple_compatible:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt",
             "-of", "json", str(final)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=30, check=True)
        streams = json.loads(probe.stdout).get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), {})
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        if (final.suffix.lower() != ".mp4" or video.get("codec_name") != "h264"
                or video.get("pix_fmt") != "yuv420p" or (audio and audio.get("codec_name") != "aac")):
            compatible = job_dir / "compatible.mp4"
            try:
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(final),
                     "-threads", "1", "-filter_threads", "1", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p",
                     "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(compatible)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=14400, check=True)
                final.unlink()
                final = job_dir / "media.mp4"
                compatible.rename(final)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                compatible.unlink(missing_ok=True)
                raise MediaError("Не удалось подготовить видео для воспроизведения на macOS.") from exc
    if final.stat().st_size > max_bytes:
        raise MediaError(
            f"Готовый файл превысил доступный лимит {max_bytes / 1024**2:.0f} МБ. "
            "Размер в карточке был приблизительным. Освободите место или выберите качество ниже."
        )
    return final


async def ffmpeg_run(args: list[str], timeout: int = 14400) -> None:
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                                                start_new_session=True)
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except BaseException as exc:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await proc.wait()
        if isinstance(exc, asyncio.TimeoutError):
            raise MediaError("Обработка заняла слишком много времени.") from exc
        raise
    if proc.returncode != 0:
        raise MediaError("FFmpeg не смог обработать этот файл.")


def parse_interval(text: str) -> tuple[int, int]:
    chunks = re.split(r"\s+|-", text.strip())
    if len(chunks) != 2:
        raise MediaError("Введите начало и конец: 00:10 01:20")
    values = []
    for chunk in chunks:
        parts = chunk.split(":")
        if len(parts) > 3 or not all(x.isdigit() for x in parts):
            raise MediaError("Время должно быть в формате секунды, ММ:СС или ЧЧ:ММ:СС.")
        total = 0
        for part in parts:
            total = total * 60 + int(part)
        values.append(total)
    if values[1] <= values[0]:
        raise MediaError("Конец фрагмента должен быть позже начала.")
    return values[0], values[1]


async def make_audio(source: Path, target: Path, codec: str, bitrate: int, interval: tuple[int, int] | None = None) -> None:
    if codec not in ("mp3", "m4a") or bitrate not in (64, 128, 192, 256, 320):
        raise MediaError("Недопустимый формат или битрейт.")
    args = []
    if interval:
        args += ["-ss", str(interval[0]), "-to", str(interval[1])]
    args += ["-i", str(source), "-vn", "-c:a", "libmp3lame" if codec == "mp3" else "aac", "-b:a", f"{bitrate}k", str(target)]
    await ffmpeg_run(args)


async def make_video(source: Path, target: Path, height: int | None, interval: tuple[int, int] | None = None) -> None:
    args = []
    if interval:
        args += ["-ss", str(interval[0]), "-to", str(interval[1])]
    args += ["-i", str(source)]
    if height:
        if height not in (240, 360, 480, 720, 1080, 1440, 2160):
            raise MediaError("Недопустимое разрешение.")
        args += ["-vf", f"scale=-2:min(ih\\,{height})"]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(target)]
    await ffmpeg_run(args)

