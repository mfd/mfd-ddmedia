from __future__ import annotations

import asyncio
import datetime
import fcntl
import html
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeChat, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, MessageEntity, ReplyParameters

from .i18n import tr
from .web.links import public_path, start_server
from .media.downloader import MediaError, estimated_size, is_portrait, normalize_media_url, offered_heights
from .media.filenames import audio_filename
from .data.storage import Storage
from .media.url_validation import validate_platform_url
from .media.ranges import parse_clip_range, MAX_CLIP_SECONDS
from .ops.error_log import configure_error_log, prune_error_logs, worker_error_stream
from .ops.bug_reports import BugReportStore, STATUSES as BUG_REPORT_STATUSES
from .ops.changelog import latest_entry
from yandex_auth import authenticate as authenticate_yandex


log = logging.getLogger("media_bot")
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}
APPROVED_USER_IDS = set(ALLOWED_USER_IDS)
DATA_DIR = Path(os.environ["DATA_DIR"])
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
LOCAL_API_URL = os.environ.get("LOCAL_API_URL", "").rstrip("/")
MAX_JOB_BYTES = int(os.environ.get("MAX_JOB_BYTES", str(3 * 1024**3)))
MIN_FREE_BYTES = int(os.environ.get("MIN_FREE_BYTES", str(1024**3)))
CRITICAL_FREE_BYTES = 100 * 1024**2
TELEGRAM_FILE_LIMIT = 1_900_000_000
LINK_TTL = 7200

DATA_DIR.mkdir(parents=True, exist_ok=True)
bug_reports = BugReportStore(DATA_DIR / "bugreports")
storage: Storage
router = Router()
queue_event = asyncio.Event()
cancelled_jobs: set[str] = set()
current_job: str | None = None
low_disk_mode = False
last_progress: dict[str, float] = {}
progress_state: dict[str, dict] = {}
heavy_job_lock = asyncio.Lock()
bot: Bot
BOT_USERNAME = ""
SCRIPT_STARTED = time.monotonic()
SCRIPT_VERSION = "2026.10.31.16.10"
BUG_REPORT_TAG = re.compile(r"(?<!\w)(?:#bug[_-]?report|bug report)\b", re.IGNORECASE)
BUG_REPORT_MAX_BYTES = 20 * 1024**2
MAX_METADATA_OUTPUT = 512 * 1024
MAX_QUEUE_LENGTH = 30
MAX_METADATA_PENDING = 4
MAX_WORKER_RSS_BYTES = int(os.environ.get("MAX_WORKER_RSS_BYTES", str(900 * 1024**2)))
metadata_pending = 0
yandex_auth_task: asyncio.Task | None = None


def duration_label(seconds: float, language: str = "ru") -> str:
    minutes = max(0, int(seconds)) // 60
    days, hours = divmod(minutes // 60, 24)
    minutes %= 60
    return tr(language, "admin_duration", days=days, hours=hours, minutes=minutes)


def server_uptime(language: str = "ru") -> str:
    try:
        return duration_label(float(Path("/proc/uptime").read_text().split()[0]), language)
    except (OSError, ValueError, IndexError):
        return tr(language, "admin_unknown")


def runtime_status(language: str = "ru") -> str:
    return (tr(language, "admin_script_version", version=SCRIPT_VERSION) + "\n"
            + tr(language, "admin_server_uptime", uptime=server_uptime(language)) + "\n"
            + tr(language, "admin_process_uptime", uptime=duration_label(time.monotonic() - SCRIPT_STARTED, language)))


def selected_format(row, language: str) -> str:
    """Return the chosen output format for user-facing download status."""
    selection = str(row["selection"] or "")
    if selection.startswith("video:"):
        quality = selection.split(":", 2)[1]
        label = f"MP4 {quality}p" if quality.isdigit() else tr(language, "format_best_video")
    elif selection.startswith("audio:"):
        label = selection.split(":", 1)[1].upper()
    else:
        label = tr(language, "format_unknown")
    return tr(language, "download_format", format=label)


def job_language(row) -> str:
    user_id = row["requester_id"] or row["owner_id"]
    return storage.settings(user_id)["language"]


def resource_status(language: str = "ru") -> str:
    disk = shutil.disk_usage(DATA_DIR)
    lines = [tr(language, "admin_disk", free=f"{disk.free / 1024**3:.1f}", total=f"{disk.total / 1024**3:.1f}")]
    media_paths = (storage.db.execute(
        "SELECT selection, file_path FROM jobs WHERE file_path IS NOT NULL "
        "AND (selection LIKE 'video:%' OR selection LIKE 'audio:%')"
    ).fetchall() if "storage" in globals() else [])
    media_bytes = {"video": 0, "audio": 0}
    for row in media_paths:
        try:
            media_bytes["audio" if row["selection"].startswith("audio:") else "video"] += Path(row["file_path"]).stat().st_size
        except OSError:
            continue
    media_total = media_bytes["audio"] + media_bytes["video"]
    lines.append(tr(language, "admin_media_total", size=storage_size_label(media_total)))
    lines.append(tr(language, "admin_media_audio", size=storage_size_label(media_bytes['audio'])))
    lines.append(tr(language, "admin_media_video", size=storage_size_label(media_bytes['video'])))
    try:
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.strip().split()[0]) * 1024
        gib = 1024**3
        lines.append(tr(language, "admin_ram", free=f"{memory['MemFree'] / gib:.2f}",
                        available=f"{memory['MemAvailable'] / gib:.2f}", total=f"{memory['MemTotal'] / gib:.2f}"))
        if memory.get("SwapTotal"):
            lines.append(tr(language, "admin_swap", free=f"{memory['SwapFree'] / gib:.2f}",
                            total=f"{memory['SwapTotal'] / gib:.2f}"))
        else:
            lines.append(tr(language, "admin_swap_unconfigured"))
    except (OSError, ValueError, KeyError, IndexError):
        lines.append(tr(language, "admin_ram_unavailable"))
    return "\n".join(lines)


async def fetch_metadata(url: str, waiting: Message | None = None, language: str = "ru") -> dict:
    global metadata_pending
    if metadata_pending >= MAX_METADATA_PENDING:
        raise MediaError("Очередь проверки ссылок заполнена. Попробуйте позже.")
    metadata_pending += 1
    try:
        return await _fetch_metadata_locked(url, waiting, language)
    finally:
        metadata_pending -= 1


async def _fetch_metadata_locked(url: str, waiting: Message | None = None, language: str = "ru") -> dict:
    async with heavy_job_lock:
        if waiting:
            try:
                await waiting.edit_text("Получаю данные о публикации…")
            except Exception as exc:
                log.warning("Could not update metadata status: %s", type(exc).__name__)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "media_bot.workers.metadata", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def read_stderr() -> str:
            output = bytearray()
            while chunk := await process.stderr.read(4096):
                if len(output) < 16_384:
                    output.extend(chunk[:16_384 - len(output)])
            return output.decode("utf-8", errors="replace")

        stderr_task = asyncio.create_task(read_stderr())
        try:
            async def read_output() -> bytes:
                output = bytearray()
                while True:
                    if process_group_rss(process.pid) > MAX_WORKER_RSS_BYTES:
                        raise MediaError("Обработка превысила лимит памяти сервера.")
                    try:
                        chunk = await asyncio.wait_for(process.stdout.read(65536), timeout=1)
                    except asyncio.TimeoutError:
                        continue
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_METADATA_OUTPUT:
                        raise MediaError("Источник вернул слишком большой ответ.")
                await process.wait()
                return bytes(output)

            output = await asyncio.wait_for(read_output(), timeout=60)
            stderr = await stderr_task
        except BaseException as exc:
            await terminate_process_group(process)
            await asyncio.gather(stderr_task, return_exceptions=True)
            if isinstance(exc, asyncio.TimeoutError):
                raise MediaError("Источник слишком долго не отвечает. Попробуйте позже.") from exc
            raise
        if process.returncode != 0:
            log.error("Metadata worker exited with code %s for %s: %s", process.returncode,
                      _safe_log_url(url), _safe_log_detail(stderr or "empty stderr"))
            raise MediaError("Не удалось получить данные. Проверьте ссылку и доступность ролика.")
        try:
            payload = json.loads(output)
        except (ValueError, UnicodeDecodeError) as exc:
            log.error("Metadata worker returned invalid JSON for %s: %s",
                      _safe_log_url(url), type(exc).__name__)
            raise MediaError("Источник вернул некорректные данные.") from exc
        if not payload.get("ok"):
            log.error("Metadata extraction failed for %s: %s", _safe_log_url(url),
                      payload.get("diagnostic", "no diagnostic")[:500])
            if payload.get("error_code") == "yandex_not_configured":
                raise MediaError(tr(language, "yandex_unavailable"), code="yandex_not_configured")
            if payload.get("error_code") == "yandex_metadata_error":
                raise MediaError(tr(language, "yandex_metadata_error"))
            if payload.get("error_code") == "spotify_unavailable":
                raise MediaError(tr(language, "spotify_unavailable"))
            raise MediaError(str(payload.get("error") or "Не удалось получить данные."))
        return payload["info"]


def _safe_log_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"[:500]


def _safe_log_detail(value: str) -> str:
    value = re.sub(r"https?://[^\s\])}<>\"']+", "[url]", value)
    value = re.sub(r"(?i)\b(token|auth|key|signature|password)\s*[:=]\s*[^\s&,;]+",
                   r"\1=[redacted]", value)
    return value[:500]


async def terminate_process_group(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


def process_group_rss(group_id: int) -> int:
    """Best-effort RSS of the worker and its children on Linux."""
    if not Path("/proc").is_dir():
        return 0
    total = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            fields = stat[stat.rfind(")") + 2:].split()
            if int(fields[2]) != group_id:
                continue
            for line in (entry / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    total += int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            continue
    return total


class MessageLogMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data: dict):
        if event.from_user and chat_allowed(event.chat.id, event.from_user.id):
            body = event.text or event.caption or ""
            body = re.sub(r"\b[0-9]{8,10}:[A-Za-z0-9_-]{35,}\b", "[скрытый токен]", body)
            body = re.sub(r"(?i)\b(api_hash|password|token)\s*[:=]\s*\S+", r"\1=[скрыто]", body)
            body = re.sub(r"(?i)([?&](?:token|key|api_key|auth|signature|session|password)=)[^&#\s]+",
                          r"\1[скрыто]", body)
            storage.log_message(event.from_user.id, event.from_user.username,
                                event.content_type, body)
        return await handler(event, data)


router.message.outer_middleware(MessageLogMiddleware())


class ProgressInputFile(FSInputFile):
    def __init__(self, path: Path, job_id: str):
        super().__init__(path)
        self.job_id = job_id

    async def read(self, bot: Bot):
        async for chunk in super().read(bot):
            state = progress_state.get(self.job_id)
            if state is not None:
                state["uploaded"] = state.get("uploaded", 0) + len(chunk)
            yield chunk


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=value) for label, value in row]
        for row in rows
    ])


def card_text(info: dict) -> str:
    def linked(label: str, url: str | None) -> str:
        safe_label = html.escape(label[:160])
        parsed = urlparse(url or "")
        if parsed.scheme in ("http", "https") and parsed.hostname and "." in parsed.hostname:
            return f'<a href="{html.escape(url or "", quote=True)}">{safe_label}</a>'
        return safe_label

    def count(value: object) -> str | None:
        if not isinstance(value, (int, float)):
            return None
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return str(int(value))

    raw_title = str(info.get("title") or "Медиа").strip()
    embedded_stats = None
    # Facebook extractors sometimes prepend engagement data to the caption,
    # e.g. "21K views · 179 reactions | Post text". Keep the title link clean
    # and show that metadata on its own line below it.
    title_stats = re.match(r"^(?P<stats>.+?)\s*\|\s*(?P<title>.+)$", raw_title)
    if (title_stats
            and re.search(r"\bviews\b", title_stats.group("stats"), re.IGNORECASE)
            and re.search(r"\breactions\b", title_stats.group("stats"), re.IGNORECASE)):
        embedded_stats = title_stats.group("stats").strip()
        raw_title = title_stats.group("title").strip()
    lines = [linked(raw_title, info.get("webpage_url") or info.get("url"))]
    if embedded_stats:
        lines.append(embedded_stats)
    metrics = []
    date = str(info.get("upload_date") or "")
    if len(date) == 8 and date.isdigit():
        try:
            published = datetime.datetime.strptime(date, "%Y%m%d")
            metrics.append(f"{published:%b} {published.day}, {published.year}")
        except ValueError:
            pass
    if info.get("duration"):
        duration = int(info["duration"])
        metrics.append(f"{duration // 3600:02d}:{duration // 60 % 60:02d}:{duration % 60:02d}")
    views = count(info.get("view_count"))
    if views is not None and not (embedded_stats and re.search(r"\bviews\b", embedded_stats, re.IGNORECASE)):
        metrics.append(f"{views} views")
    if metrics:
        lines.append(" · ".join(metrics))
    reactions = []
    for key, label in (("like_count", "likes"), ("comment_count", "comments")):
        if key == "like_count" and embedded_stats and re.search(r"\breactions\b", embedded_stats, re.IGNORECASE):
            continue
        value = count(info.get(key))
        if value is not None:
            reactions.append(f"{value} {label}")
    if reactions:
        lines.append(" · ".join(reactions))
    if info.get("channel"):
        lines.append(linked(info["channel"], info.get("channel_url")))
    followers = count(info.get("channel_follower_count"))
    if followers is not None:
        lines.append(f"{followers} subscribers")
    lines.append("\n<b>Видео</b>")
    heights = offered_heights(info)
    orientation = ":portrait" if is_portrait(info) else ""
    for number, height in enumerate(heights, 1):
        lines.append(f"{number}. <i>mp4, {height}p</i> [{size_label(estimated_size(info, f'video:{height}{orientation}'))}]")
    if not heights and any(f.get("vcodec") not in (None, "none") for f in info.get("formats", [])):
        lines.append(f"1. <i>mp4, лучшее качество</i> [{size_label(estimated_size(info, 'video:best'))}]")
    lines.append("\n<b>Аудио</b>")
    for number, ext in enumerate(("m4a", "mp3"), len(heights) + 1):
        lines.append(f"{number}. <i>{ext.upper()}</i> [{size_label(estimated_size(info, f'audio:{ext}'))}]")
    dimension = "width" if is_portrait(info) else "height"
    needs_conversion = [height for height in heights if not any(
        fmt.get(dimension) == height and (
            str(fmt.get("vcodec") or "").startswith("avc1") or fmt.get("vcodec") == "h264")
        for fmt in info.get("formats", []) if fmt.get("vcodec") not in (None, "none"))]
    if needs_conversion:
        labels = ", ".join(f"{height}p" for height in needs_conversion)
        lines.append(f"\nИсходные форматы {labels} несовместимы со стандартным плеером Apple iPhone. Бот преобразует их в MP4 с H.264 и AAC.")
    if any(height > 1080 for height in heights):
        lines.append("На старых устройствах Apple 1440p и 2160p могут не воспроизводиться; выберите 1080p или ниже.")
    lines.append("Размеры примерные. Выберите формат:")
    return "\n".join(line for line in lines if line)


def size_label(size: int | None) -> str:
    if size is None:
        return "размер неизвестен"
    if size < 1024**2:
        return f"≈{size / 1024:.1f} КБ"
    mb = size / 1024**2
    return f"≈{mb:.0f} МБ" if mb >= 100 else f"≈{mb:.1f} МБ"


def storage_size_label(size: int) -> str:
    """Use the requested compact Gb/Mb format for stored media totals."""
    if size < 1024**3:
        amount, unit = size / 1024**2, "Mb"
    else:
        amount, unit = size / 1024**3, "Gb"
    return f"{amount:.1f}".rstrip("0").rstrip(".") + f" {unit}"


def description_split_at(description: str, limit: int) -> int:
    """Keep a line or word intact when a Telegram caption/message fills up."""
    if len(description) <= limit:
        return len(description)
    if limit <= 0:
        return 0
    prefix = description[:limit]
    line_end = prefix.rfind("\n")
    if line_end >= limit // 2:
        return line_end + 1
    word_end = max(prefix.rfind(" "), prefix.rfind("\t"), line_end)
    return word_end + 1 if word_end >= limit // 2 else limit


def description_chunks(description: str, limit: int = 3900):
    remaining = description.strip()
    while remaining:
        cut = description_split_at(remaining, limit)
        yield remaining[:cut].strip()
        remaining = remaining[cut:].strip()


async def send_description_continuation(chat_id: int, description: str, media_message_id: int) -> None:
    for chunk in description_chunks(description):
        await bot.send_message(chat_id, chunk, disable_web_page_preview=True,
                               reply_parameters=ReplyParameters(message_id=media_message_id))


def result_caption(row, server_url: str, actual_size: int, *, include_server_link: bool = False) -> tuple[str, str]:
    info = json.loads(row["metadata"])
    settings = storage.settings(row["requester_id"] or row["owner_id"])
    is_audio = row["selection"].startswith("audio:")
    display_title = audio_output_title(row, info) if is_audio else row["title"]
    if len(display_title) > 160:
        # Keep the compact trim range visible even when a long title is shortened.
        range_suffix = re.search(r"( – \[\d{4}-\d{4}m\])$", display_title) if is_audio else None
        if range_suffix:
            display_title = display_title[:160 - len(range_suffix.group(1))].rstrip() + range_suffix.group(1)
        else:
            display_title = display_title[:160]
    title = html.escape(display_title)
    source = info.get("webpage_url") or row["url"]
    title = f'<a href="{html.escape(source, quote=True)}">{title}</a>'
    lines = [] if settings["hide_title"] else [title]
    date = str(info.get("upload_date") or "")
    metrics = []
    if len(date) == 8 and date.isdigit():
        try:
            published = datetime.datetime.strptime(date, "%Y%m%d")
            metrics.append(f"{published:%b} {published.day}, {published.year}")
        except ValueError:
            pass
    if info.get("duration"):
        seconds = int(info["duration"])
        metrics.append(f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}")
    for key, label in (("view_count", "views"),):
        value = info.get(key)
        if isinstance(value, (int, float)):
            amount = f"{value / 1_000:.1f}K" if value >= 1_000 else str(int(value))
            metrics.append(f"{amount} {label}")
    if metrics and not settings["hide_meta"]:
        lines.append(" · ".join(metrics))
    reactions = []
    for key, label in (("like_count", "likes"), ("comment_count", "comments")):
        value = info.get(key)
        if isinstance(value, (int, float)):
            amount = f"{value / 1_000:.1f}K" if value >= 1_000 else str(int(value))
            reactions.append(f"{amount} {label}")
    if reactions and not settings["hide_meta"]:
        lines.append(" · ".join(reactions))
    # For audio, the artist is already part of the linked ``Artist - Track`` title.
    channel = None if is_audio else info.get("channel")
    if channel and not settings["hide_meta"]:
        channel_url = info.get("channel_url")
        safe_channel = html.escape(str(channel)[:100])
        if channel_url and urlparse(channel_url).scheme in ("http", "https"):
            safe_channel = f'<a href="{html.escape(channel_url, quote=True)}">{safe_channel}</a>'
        lines.append(safe_channel)
    followers = info.get("channel_follower_count")
    if isinstance(followers, (int, float)) and not settings["hide_meta"]:
        amount = f"{followers / 1_000:.1f}K" if followers >= 1_000 else str(int(followers))
        lines.append(f"{amount} subscribers")
    selection = row["selection"]
    if selection.startswith("video:"):
        quality = selection.split(":")[1]
        label = f"mp4 {quality}p" if quality.isdigit() else "mp4"
    else:
        label = selection.split(":", 1)[-1].upper()
    if not settings["hide_meta"]:
        lines.append(f"<i>{html.escape(label)} [{actual_size / 1024**2:.1f} МБ]</i>")
    header = "\n".join(lines)
    description = "" if settings["hide_description"] else str(info.get("description") or "").strip()
    download_line = (f'<a href="{html.escape(server_url, quote=True)}">Ссылка на файл</a>'
                     if server_url and include_server_link else "")
    if not description:
        return header + (("\n\n" if header else "") + download_line if download_line else ""), ""
    visible_header = html.unescape(re.sub(r"<[^>]*>", "", header))
    available = max(0, 980 - len(visible_header) - len("Ссылка на файл") - 4)
    cut = description_split_at(description, available)
    first = description[:cut].rstrip()
    shown = html.escape(first)
    if shown and settings["collapse_description"] and len(description) <= available:
        shown = f"<blockquote expandable>{shown}</blockquote>"
    caption = header + (("\n\n" if header else "") + shown if shown else "")
    if download_line:
        caption += "\n\n" + download_line
    return caption, description[cut:].lstrip()


def format_audio_range(start: int, end: int) -> str:
    def stamp(seconds: int) -> str:
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"
    return f"{stamp(start)}–{stamp(end)}"


def compact_audio_range(start: int, end: int) -> str:
    """Format a clip range as ``[MMSS-MMSSm]`` for names and captions."""
    def stamp(seconds: int) -> str:
        minutes, seconds = divmod(max(0, int(seconds)), 60)
        return f"{minutes:02d}{seconds:02d}"
    return f"[{stamp(start)}-{stamp(end)}m]"


def audio_output_title(row, info: dict) -> str:
    artist = str(info.get("channel") or "").strip()
    title = str(row["title"] or "media").strip()
    if artist and artist.casefold() not in title.casefold():
        title = f"{artist} - {title}"
    edit = info.get("_audio_edit") or {}
    start, end = int(edit.get("start") or 0), int(edit.get("end") or 0)
    if end > start:
        title += f" – {compact_audio_range(start, end)}"
    return title


def progress_bar(amount: int, total: int | None) -> str:
    percent = min(100, int(amount * 100 / total)) if total else 0
    filled = min(10, percent // 10)
    return f"[{ '■' * filled}{'□' * (10 - filled)}] {percent}%"


def card_buttons(job_id: str, info: dict, language: str = "ru") -> InlineKeyboardMarkup:
    heights = offered_heights(info)
    rows = [[(f"{height}p", f"sel:{job_id}:v{height}") for height in heights[i:i + 3]]
            for i in range(0, len(heights), 3)]
    if not heights and any(f.get("vcodec") not in (None, "none") for f in info.get("formats", [])):
        rows.append([("Лучшее качество", f"sel:{job_id}:vbest")])
    rows.append([("Аудио M4A", f"sel:{job_id}:am4a"), ("Аудио MP3", f"sel:{job_id}:amp3")])
    if heights or any(f.get("vcodec") not in (None, "none") for f in info.get("formats", [])):
        rows.append([(tr(language, "clip_button"), f"clip:{job_id}")])
    return keyboard(rows)


def clip_format_buttons(job_id: str, info: dict, language: str) -> InlineKeyboardMarkup:
    heights = offered_heights(info)
    rows = [[(f"{height}p", f"clipfmt:{job_id}:v{height}") for height in heights[i:i + 3]]
            for i in range(0, len(heights), 3)]
    if not heights and any(f.get("vcodec") not in (None, "none") for f in info.get("formats", [])):
        rows.append([("Лучшее качество" if language == "ru" else "Best quality", f"clipfmt:{job_id}:vbest")])
    rows.append([(tr(language, "clip_cancel_button"), f"clipcancel:{job_id}")])
    return keyboard(rows)


def result_buttons(job_id: str, is_audio: bool) -> InlineKeyboardMarkup:
    if is_audio:
        return keyboard([[ ("Скачать заново", f"again:{job_id}") ],
                         [("Обрезать / сменить формат / битрейт", f"audioedit:{job_id}")]])
    return keyboard([[ ("Скачать заново", f"again:{job_id}") ],
                     [("Извлечь аудио", f"audio:{job_id}") ],
                     [("Редактировать видео", f"videoedit:{job_id}")]])


def is_owner(user_id: int | None) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


def allowed(user_id: int | None) -> bool:
    return is_owner(user_id) or (user_id is not None and user_id in APPROVED_USER_IDS)


def chat_allowed(chat_id: int, user_id: int | None) -> bool:
    return ((chat_id == user_id and allowed(user_id))
            or (chat_id == OWNER_ID and is_owner(user_id))
            or storage.group_enabled(chat_id))


def callback_allowed(query: CallbackQuery, row) -> bool:
    return bool(row and query.message and row["owner_id"] == query.message.chat.id
                and chat_allowed(query.message.chat.id, query.from_user.id))


def disk_critical() -> bool:
    return shutil.disk_usage(DATA_DIR).free < CRITICAL_FREE_BYTES


def available_file_limit(free_bytes: int) -> int:
    """Maximum file size now, after keeping the configured disk reserve."""
    return min(MAX_JOB_BYTES, max(0, free_bytes - MIN_FREE_BYTES))


def capacity_message(estimate: int | None, limit: int) -> str:
    limit_mb = max(0, limit) / 1024**2
    if estimate is not None and estimate > limit:
        estimate_mb = estimate / 1024**2
        reason = ("с учётом резерва диска" if limit < MAX_JOB_BYTES else "по настройке сервера")
        return (f"Выбранный файл оценивается в {estimate_mb:.0f} МБ, а лимит задачи — "
                f"{limit_mb:.0f} МБ {reason}. Выберите качество ниже или увеличьте лимит и место на сервере.")
    return (f"Фактический размер превысил доступный лимит {limit_mb:.0f} МБ. "
            "Размер в карточке был приблизительным. Выберите качество ниже или проверьте лимит задачи.")


def unavailable_text(chat_id: int) -> str:
    if chat_id == OWNER_ID:
        free = shutil.disk_usage(DATA_DIR).free / 1024**2
        return f"На сервере осталось {free:.0f} МБ свободного места. Бот временно недоступен."
    return "Бот временно недоступен: на сервере идут технические работы."


async def change_status(job_id: str, text: str, *, force: bool = False, link_url: str = "") -> None:
    now = time.monotonic()
    if not force and now - last_progress.get(job_id, 0) < 4:
        return
    row = storage.get(job_id)
    if not row:
        return
    if not is_owner(row["requester_id"] or row["owner_id"]):
        link_url = ""
    if not text.startswith(row["title"] + "\n\n"):
        text = row["title"] + "\n\n" + text.removeprefix(row["title"]).lstrip("\n")
    last_progress[job_id] = now
    entities: list[MessageEntity] = []
    if text.startswith(row["title"]):
        entities.append(MessageEntity(type="text_link", offset=0,
                                      length=len(row["title"].encode("utf-16-le")) // 2,
                                      url=row["url"]))
    if link_url:
        prefix = text + "\n"
        entities.append(MessageEntity(type="text_link", offset=len(prefix.encode("utf-16-le")) // 2,
                                      length=len("Ссылка на файл"), url=link_url))
        text = prefix + "Ссылка на файл"
    markup = keyboard([[("Отменить", f"cancel:{job_id}")]]) if row["status"] in ("queued", "downloading", "processing", "uploading") else None
    try:
        if row["status_message_id"]:
            if row["status_is_photo"]:
                await bot.edit_message_caption(caption=text, chat_id=row["owner_id"], message_id=row["status_message_id"],
                                               caption_entities=entities, reply_markup=markup)
            else:
                await bot.edit_message_text(text, chat_id=row["owner_id"], message_id=row["status_message_id"],
                                            entities=entities, reply_markup=markup)
        else:
            message = await bot.send_message(row["owner_id"], text, entities=entities, reply_markup=markup)
            storage.update(job_id, status_message_id=message.message_id)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            log.warning("Status update failed for %s: %s", job_id, str(exc)[:300])
            if row["status_message_id"]:
                try:
                    message = await bot.send_message(row["owner_id"], text, entities=entities, reply_markup=markup)
                    storage.update(job_id, status_message_id=message.message_id, status_is_photo=0)
                except Exception as fallback_exc:
                    log.warning("Fallback status failed for %s: %s", job_id, type(fallback_exc).__name__)


def language_buttons() -> InlineKeyboardMarkup:
    return keyboard([[('Русский', 'menu:lang:ru'), ('English', 'menu:lang:en')]])


def menu_buttons(language: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=tr(language, "menu_files"), callback_data='menu:files:all:0')],
        [InlineKeyboardButton(text=tr(language, "menu_settings"), callback_data='menu:settings')],
        [InlineKeyboardButton(text=tr(language, "menu_language"), callback_data='menu:language')],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def welcome_text(user_id: int, language: str) -> str:
    return tr(language, "welcome").format(user_id=user_id)


def settings_text(language: str, settings: dict) -> str:
    return tr(language, "settings")


def settings_buttons(language: str, settings: dict) -> InlineKeyboardMarkup:
    rows = []
    for key in ("auto_download", "apple_compatible", "hide_title", "hide_meta",
                "hide_description", "collapse_description"):
        state = tr(language, "on" if settings[key] else "off")
        rows.append([(f"{state} · {tr(language, 'setting_' + key)}", f"menu:setting:{key}")])
    rows.append([(tr(language, "back"), "menu:home")])
    return keyboard(rows)


def files_view(user_id: int, language: str, kind: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    kind = kind if kind in {"all", "audio", "video"} else "all"
    count = storage.user_file_count(user_id, kind)
    page_count = max(1, (count + 7) // 8)
    page = max(0, min(page, page_count - 1))
    rows = storage.user_files(user_id, page * 8, 8, kind)
    lines = [f"<b>{tr(language, 'files_heading')}</b>", tr(language, "files_retention"), ""]
    buttons: list[list[InlineKeyboardButton]] = []
    if not rows:
        lines.append(tr(language, "files_empty"))
    for index, row in enumerate(rows, start=page * 8 + 1):
        info = json.loads(row["metadata"])
        title = html.escape(row["title"][:100])
        source = html.escape(row["url"], quote=True)
        date = datetime.datetime.fromtimestamp(row["created_at"]).strftime("%d.%m.%Y · %H:%M")
        size = size_label(row["file_size"] or 0) if row["file_size"] else "—"
        kind_label = row["selection"].replace("video:", "mp4 ").replace("audio:", "")
        lines.append(f'{index}. <a href="{source}">{title}</a>')
        details = [date]
        channel = str(info.get("channel") or "").strip()
        if channel:
            details.append(html.escape(channel[:50]))
        details.extend((size, html.escape(kind_label)))
        lines.append(" · ".join(details))
        if (is_owner(user_id) and row["file_path"] and row["token"] and row["link_expires_at"]
                and row["link_expires_at"] > int(time.time()) and Path(row["file_path"]).is_file()
                and PUBLIC_BASE_URL):
            file_link = html.escape(PUBLIC_BASE_URL + public_path(row), quote=True)
            lines.append(f'<a href="{file_link}">{tr(language, "file_link")}</a>')
        elif is_owner(user_id):
            lines.append(tr(language, "link_expired"))
        lines.append("")
    buttons.append([
        InlineKeyboardButton(text=tr(language, "audio"), callback_data="menu:files:audio:0"),
        InlineKeyboardButton(text=tr(language, "video"), callback_data="menu:files:video:0"),
        InlineKeyboardButton(text=tr(language, "all"), callback_data="menu:files:all:0"),
    ])
    if page_count > 1:
        navigation = []
        if page > 0:
            navigation.append(InlineKeyboardButton(text="←", callback_data=f"menu:files:{kind}:{page - 1}"))
        navigation.append(InlineKeyboardButton(text=f"{page + 1}/{page_count}", callback_data="menu:noop"))
        if page + 1 < page_count:
            navigation.append(InlineKeyboardButton(text="→", callback_data=f"menu:files:{kind}:{page + 1}"))
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton(text=tr(language, "clear_storage"),
                                         callback_data="menu:clear:confirm")])
    buttons.append([InlineKeyboardButton(text=tr(language, "back"), callback_data="menu:home")])
    return "\n".join(lines)[:3900], InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not message.from_user:
        return
    if message.chat.type in ("group", "supergroup"):
        if is_owner(message.from_user.id):
            storage.enable_group(message.chat.id)
            await message.answer("Бот включён в этом чате. Отправьте /dd и ссылку на видео. Файл и временная ссылка появятся здесь после скачивания.")
        elif storage.group_enabled(message.chat.id):
            await message.answer("Чтобы скачать видео, отправьте /dd и ссылку.")
        return
    if OWNER_ID == 0:
        await message.answer(f"Ваш Telegram ID: {message.from_user.id}. Сообщите его владельцу сервера для настройки доступа.")
    elif allowed(message.from_user.id):
        if not storage.has_language(message.from_user.id):
            await message.answer("Выберите язык / Choose your language", reply_markup=language_buttons())
            return
        language = storage.settings(message.from_user.id)["language"]
        await message.answer(welcome_text(message.from_user.id, language), parse_mode="HTML",
                             reply_markup=menu_buttons(language))
    else:
        language = storage.settings(message.from_user.id)["language"]
        await message.answer(
            tr(language, "access_intro"),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=tr(language, "access_request_button"),
                                     callback_data="access:request")
            ]]),
        )


@router.callback_query(F.data == "access:request")
async def access_request_callback(query: CallbackQuery) -> None:
    if not query.message or query.message.chat.type != "private":
        await query.answer("Откройте бота в личном чате", show_alert=True)
        return
    user = query.from_user
    if allowed(user.id):
        await query.answer("Доступ уже есть", show_alert=True)
        return
    status = storage.request_access(user.id, user.username, user.full_name)
    language = storage.settings(user.id)["language"]
    if status == "new":
        name = html.escape(user.full_name[:200])
        username = f" (@{html.escape(user.username)})" if user.username else ""
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=tr("ru", "access_approve"),
                                 callback_data=f"access:approve:{user.id}"),
            InlineKeyboardButton(text=tr("ru", "access_deny"),
                                 callback_data=f"access:deny:{user.id}"),
        ]])
        await bot.send_message(
            OWNER_ID,
            tr("ru", "access_admin_request", name=name, username=username, user_id=user.id),
            parse_mode="HTML", reply_markup=markup,
        )
    await query.answer()
    await query.message.answer(tr(language, f"access_{status}"))


@router.callback_query(F.data.startswith("access:approve:") | F.data.startswith("access:deny:"))
async def access_decision_callback(query: CallbackQuery) -> None:
    if (not is_owner(query.from_user.id) or not query.message
            or query.message.chat.type != "private"):
        await query.answer(tr("ru", "admin_access_denied"), show_alert=True)
        return
    _, action, target_text = (query.data or "").split(":")
    if not target_text.isdigit():
        await query.answer("Некорректный запрос", show_alert=True)
        return
    target_id = int(target_text)
    if storage.access_status(target_id) != "pending":
        await query.answer(tr("ru", "access_already_handled"), show_alert=True)
        return
    if action == "approve":
        storage.approve_user(target_id)
        APPROVED_USER_IDS.add(target_id)
        await bot.send_message(target_id, tr("ru", "access_granted_user"))
        result_key = "access_approved_admin"
    else:
        storage.deny_access(target_id)
        await bot.send_message(target_id, tr("ru", "access_denied_user"))
        result_key = "access_denied_admin"
    await query.message.edit_text(tr("ru", result_key, user_id=target_id))
    await query.answer()


@router.callback_query(F.data == "changelog:latest")
async def latest_changelog_callback(query: CallbackQuery) -> None:
    if (not is_owner(query.from_user.id) or not query.message
            or query.message.chat.type != "private"):
        await query.answer(tr("ru", "admin_access_denied"), show_alert=True)
        return
    language = storage.settings(query.from_user.id)["language"]
    try:
        contents = (Path(__file__).resolve().parents[1] / "CHANGELOG.txt").read_text(encoding="utf-8")
    except OSError:
        await query.answer(tr(language, "changelog_unavailable"), show_alert=True)
        return
    entry = latest_entry(contents)
    if not entry or not entry[1]:
        await query.answer(tr(language, "changelog_empty"), show_alert=True)
        return
    version, changes = entry
    await query.answer()
    message = tr(language, "changelog_latest_heading", version=version) + "\n\n" + changes
    for chunk in description_chunks(message):
        await bot.send_message(query.message.chat.id, chunk, disable_web_page_preview=True,
                               reply_parameters=ReplyParameters(message_id=query.message.message_id))


@router.callback_query(F.data.startswith("menu:"))
async def menu_callback(query: CallbackQuery) -> None:
    if not query.message or query.message.chat.type != "private" or not allowed(query.from_user.id):
        await query.answer("Нет доступа", show_alert=True)
        return
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    user_id = query.from_user.id
    if action == "lang" and len(parts) == 3 and parts[2] in {"ru", "en"}:
        storage.set_language(user_id, parts[2])
        language = parts[2]
        await query.message.edit_text(welcome_text(user_id, language), parse_mode="HTML",
                                      reply_markup=menu_buttons(language))
    elif action == "language":
        await query.message.edit_text("Выберите язык / Choose your language", reply_markup=language_buttons())
    elif action == "home":
        language = storage.settings(user_id)["language"]
        await query.message.edit_text(welcome_text(user_id, language), parse_mode="HTML",
                                      reply_markup=menu_buttons(language))
    elif action == "settings":
        settings = storage.settings(user_id)
        language = settings["language"]
        await query.message.edit_text(settings_text(language, settings), parse_mode="HTML",
                                      reply_markup=settings_buttons(language, settings))
    elif action == "setting" and len(parts) == 3 and parts[2] in Storage.SETTING_KEYS:
        storage.toggle_setting(user_id, parts[2])
        settings = storage.settings(user_id)
        language = settings["language"]
        await query.message.edit_text(settings_text(language, settings), parse_mode="HTML",
                                      reply_markup=settings_buttons(language, settings))
    elif action == "files" and len(parts) == 4 and parts[2] in {"all", "audio", "video"}:
        try:
            page = max(0, min(1000, int(parts[3])))
        except ValueError:
            await query.answer("Неверная страница", show_alert=True)
            return
        language = storage.settings(user_id)["language"]
        body, buttons = files_view(user_id, language, parts[2], page)
        await query.message.edit_text(body, parse_mode="HTML", reply_markup=buttons,
                                      disable_web_page_preview=True)
    elif action == "clear" and len(parts) == 3 and parts[2] == "confirm":
        language = storage.settings(user_id)["language"]
        await query.message.edit_text(tr(language, "clear_confirm"), reply_markup=keyboard([
            [(tr(language, "delete"), "menu:clear:yes")],
            [(tr(language, "back"), "menu:files:all:0")],
        ]))
    elif action == "clear" and len(parts) == 3 and parts[2] == "yes":
        paths = storage.clear_user_files(user_id)
        for path in paths:
            try:
                path.unlink(missing_ok=True)
                shutil.rmtree(path.parent, ignore_errors=True)
            except OSError:
                log.warning("Could not remove stored user file: %s", path.name)
        language = storage.settings(user_id)["language"]
        body, buttons = files_view(user_id, language, "all", 0)
        await query.message.edit_text(body, parse_mode="HTML", reply_markup=buttons,
                                      disable_web_page_preview=True)
    elif action == "noop":
        pass
    else:
        await query.answer("Действие недоступно", show_alert=True)
        return
    await query.answer()


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    if message.from_user and chat_allowed(message.chat.id, message.from_user.id):
        await message.answer("Пришлите ссылку HTTPS на видео. В группе используйте /dd и ссылку. /status показывает состояние; /cancel отменяет текущую задачу.")


@router.message(Command("enable_group"))
async def enable_group(message: Message) -> None:
    if message.chat.type not in ("group", "supergroup"):
        return
    if not message.from_user or not is_owner(message.from_user.id):
        await message.answer("Включить бота в группе может только владелец.")
        return
    storage.enable_group(message.chat.id)
    await message.answer("Бот включён в этом чате. Отправьте /dd и ссылку на видео, затем выберите качество. Файл появится здесь после скачивания.")


@router.message(Command("status"))
async def status_command(message: Message) -> None:
    if not message.from_user or not is_owner(message.from_user.id):
        return
    queued = storage.db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
    await message.answer(
        f"В работе: {current_job or 'нет'}\n"
        f"В очереди: {queued}"
    )


def admin_buttons(language: str = "ru") -> InlineKeyboardMarkup:
    yandex_status = tr(language, "admin_yandex_connected" if os.environ.get("YANDEX_MUSIC_TOKEN", "").strip()
                        else "admin_yandex_disconnected")
    rows = [
        [(tr(language, "admin_stats"), "adm:stats"), (tr(language, "admin_history"), "adm:history:0")],
        [(tr(language, "admin_files"), "adm:files:0")],
        [(tr(language, "admin_yandex_button", status=yandex_status), "adm:yandex")],
        [(tr(language, "admin_bug_reports"), "adm:bugs:active:0")],
        [(tr(language, "admin_clean"), "adm:clean")],
    ]
    buttons = keyboard(rows).inline_keyboard
    if PUBLIC_BASE_URL:
        buttons.append([InlineKeyboardButton(text=tr(language, "admin_changelog"), url=PUBLIC_BASE_URL + "/CHANGELOG.txt")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def bug_report_list_screen(language: str, filter_name: str, offset: int) -> tuple[str, InlineKeyboardMarkup]:
    reports = bug_reports.list()
    counts = {status: sum(report["status"] == status for report in reports)
              for status in BUG_REPORT_STATUSES}
    if filter_name == "active":
        selected = [report for report in reports if report["status"] != "closed"]
    elif filter_name == "closed":
        selected = [report for report in reports if report["status"] == "closed"]
    else:
        selected = reports
        filter_name = "all"
    offset = max(0, min(10000, offset))
    page = selected[offset:offset + 10]
    rows = []
    for report in page:
        preview = " ".join(bug_reports.description(report["number"]).split())[:35]
        label = tr(language, f"bug_status_{report['status']}")
        rows.append([(f"{report['ticket']} · {label} · {preview}",
                      f"adm:bug:{report['number']}:{filter_name}:{offset}")])
    navigation = []
    if offset:
        navigation.append((tr(language, "admin_newer"), f"adm:bugs:{filter_name}:{max(0, offset - 10)}"))
    if len(selected) > offset + 10:
        navigation.append((tr(language, "admin_older"), f"adm:bugs:{filter_name}:{offset + 10}"))
    if navigation:
        rows.append(navigation)
    rows.append([(tr(language, "bug_filter_active"), "adm:bugs:active:0"),
                 (tr(language, "bug_filter_closed"), "adm:bugs:closed:0")])
    rows.append([(tr(language, "admin_back"), "adm:back")])
    heading = tr(language, "bug_list_heading", open=counts["open"],
                 in_progress=counts["in_progress"], closed=counts["closed"])
    if not page:
        heading += "\n\n" + tr(language, "bug_list_empty")
    return heading, keyboard(rows)


def bug_report_detail_screen(language: str, ticket_id: int, filter_name: str,
                             offset: int) -> tuple[str, InlineKeyboardMarkup] | None:
    report = bug_reports.get(ticket_id)
    if not report or report["status"] not in BUG_REPORT_STATUSES:
        return None
    description = bug_reports.description(ticket_id)
    rows = []
    if report.get("screenshot"):
        rows.append([(tr(language, "bug_show_screenshot"), f"adm:bug_photo:{ticket_id}")])
    rows.extend([[(tr(language, f"bug_set_{status}"),
                   f"adm:bug_set:{ticket_id}:{status}:{filter_name}:{offset}")]
                 for status in ("open", "in_progress", "closed") if status != report["status"]])
    rows.append([(tr(language, "admin_back"), f"adm:bugs:{filter_name}:{offset}")])
    text = tr(language, "bug_detail_heading", ticket=report["ticket"],
              status=tr(language, f"bug_status_{report['status']}"),
              created=report["created_at"]) + "\n\n" + description
    return text, keyboard(rows)


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if message.chat.type != "private" or not message.from_user or not is_owner(message.from_user.id):
        return
    language = storage.settings(message.from_user.id)["language"]
    await message.answer(f"{tr(language, 'admin_panel_heading')}\n\n{runtime_status(language)}\n{resource_status(language)}",
                         reply_markup=admin_buttons(language))


@router.callback_query(F.data.startswith("adm:"))
async def admin_callback(query: CallbackQuery) -> None:
    language = storage.settings(query.from_user.id)["language"]
    if not is_owner(query.from_user.id) or not query.message or query.message.chat.type != "private":
        await query.answer(tr(language, "admin_access_denied"), show_alert=True)
        return
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "back":
        await query.message.edit_text(f"{tr(language, 'admin_panel_heading')}\n\n{runtime_status(language)}\n{resource_status(language)}",
                                      reply_markup=admin_buttons(language))
    elif action == "bugs":
        filter_name = parts[2] if len(parts) > 2 and parts[2] in {"active", "closed", "all"} else "active"
        try:
            offset = int(parts[3])
        except (IndexError, ValueError):
            offset = 0
        text, markup = bug_report_list_screen(language, filter_name, offset)
        await query.message.edit_text(text, reply_markup=markup)
    elif action in {"bug", "bug_set", "bug_photo"}:
        try:
            ticket_id = int(parts[2])
        except (IndexError, ValueError):
            await query.answer(tr(language, "bug_missing"), show_alert=True)
            return
        if action == "bug_photo":
            report = bug_reports.get(ticket_id)
            screenshot_name = report.get("screenshot") if report else None
            image = (bug_reports.folder(ticket_id) / screenshot_name
                     if screenshot_name in {"screenshot.jpg", "screenshot.png", "screenshot.webp"} else None)
            if not image or not image.is_file():
                await query.answer(tr(language, "bug_missing"), show_alert=True)
                return
            await bot.send_photo(query.message.chat.id, FSInputFile(image), caption=report["ticket"])
        else:
            if action == "bug_set":
                status = parts[3] if len(parts) > 3 else ""
                if status not in BUG_REPORT_STATUSES:
                    await query.answer(tr(language, "bug_missing"), show_alert=True)
                    return
                try:
                    bug_reports.set_status(ticket_id, status)
                except ValueError:
                    await query.answer(tr(language, "bug_missing"), show_alert=True)
                    return
                filter_name = parts[4] if len(parts) > 4 and parts[4] in {"active", "closed", "all"} else "active"
                offset_text = parts[5] if len(parts) > 5 else "0"
            else:
                filter_name = parts[3] if len(parts) > 3 and parts[3] in {"active", "closed", "all"} else "active"
                offset_text = parts[4] if len(parts) > 4 else "0"
            try:
                offset = int(offset_text)
            except ValueError:
                offset = 0
            screen = bug_report_detail_screen(language, ticket_id, filter_name, offset)
            if not screen:
                await query.answer(tr(language, "bug_missing"), show_alert=True)
                return
            await query.message.edit_text(screen[0], reply_markup=screen[1])
    elif action == "yandex":
        configured = bool(os.environ.get("YANDEX_MUSIC_TOKEN", "").strip())
        status = tr(language, "admin_yandex_status_connected" if configured else "admin_yandex_status_disconnected")
        await query.message.edit_text(
            f"{tr(language, 'admin_yandex_heading')}\n\n{status}\n\n{tr(language, 'admin_yandex_instructions')}",
            reply_markup=keyboard([[(tr(language, 'admin_yandex_connect'), 'adm:yandex_connect')],
                                  [(tr(language, 'admin_back'), 'adm:back')]]),
        )
    elif action == "yandex_connect":
        global yandex_auth_task
        if yandex_auth_task and not yandex_auth_task.done():
            await query.answer(tr(language, "admin_yandex_already_running"), show_alert=True)
            return
        await query.message.edit_text(
            tr(language, "admin_yandex_starting"),
            reply_markup=keyboard([[(tr(language, 'admin_back'), 'adm:yandex')]]),
        )
        loop = asyncio.get_running_loop()

        def send_device_code(code) -> None:
            text = tr(language, "admin_yandex_dm", url=code.verification_url,
                      code=html.escape(str(code.user_code)))
            future = asyncio.run_coroutine_threadsafe(
                bot.send_message(OWNER_ID, text, parse_mode="HTML", disable_web_page_preview=True), loop)
            future.result(timeout=20)

        async def authorize_yandex() -> None:
            try:
                token = await asyncio.to_thread(authenticate_yandex, send_device_code)
                os.environ["YANDEX_MUSIC_TOKEN"] = token
                await bot.send_message(OWNER_ID,
                                       tr(language, "admin_yandex_auth_success"))
                log.info("Yandex Music device authorization completed")
            except Exception as exc:
                log.error("Yandex Music device authorization failed: %s", type(exc).__name__)
                await bot.send_message(OWNER_ID,
                                       tr(language, "admin_yandex_failed"))

        yandex_auth_task = asyncio.create_task(authorize_yandex())
    elif action == "stats":
        users, messages, last_week = storage.message_stats()
        queued = storage.db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
        completed = storage.db.execute("SELECT count(*) FROM jobs WHERE status='sent'").fetchone()[0]
        temp_files = storage.db.execute("SELECT count(*) FROM jobs WHERE file_path IS NOT NULL").fetchone()[0]
        free = shutil.disk_usage(DATA_DIR).free / 1024**3
        await query.message.edit_text(
            f"{tr(language, 'admin_stats_heading')}\n{tr(language, 'admin_stats_users', count=users)}\n"
            f"{tr(language, 'admin_stats_messages', count=messages)}\n{tr(language, 'admin_stats_week', count=last_week)}\n"
            f"{tr(language, 'admin_stats_sent', count=completed)}\n{tr(language, 'admin_stats_queue', count=queued)}\n"
            f"{tr(language, 'admin_stats_temp', count=temp_files)}\n{tr(language, 'admin_disk', free=f'{free:.1f}', total=f'{shutil.disk_usage(DATA_DIR).total / 1024**3:.1f}')}\n\n"
            f"{runtime_status(language)}\n\n{tr(language, 'admin_online_unknown')}",
            reply_markup=keyboard([[(tr(language, 'admin_back'), 'adm:back')]]),
        )
    elif action == "history":
        try:
            offset = max(0, min(10000, int(parts[2])))
        except (IndexError, ValueError):
            offset = 0
        rows = storage.history_users(offset, 11)
        buttons = [[(f"{('@' + row['username']) if row['username'] else row['user_id']} · {row['messages']}",
                     f"adm:history_user:{row['user_id']}:0:{offset}")]
                   for row in rows[:10]]
        controls = []
        if offset:
            controls.append((tr(language, "admin_newer"), f"adm:history:{max(0, offset - 10)}"))
        if len(rows) > 10:
            controls.append((tr(language, "admin_older"), f"adm:history:{offset + 10}"))
        if controls:
            buttons.append(controls)
        buttons.append([(tr(language, "admin_back"), "adm:back")])
        await query.message.edit_text(tr(language, "admin_history_choose")
                                      if rows else tr(language, "admin_history_empty"), reply_markup=keyboard(buttons))
    elif action == "history_user":
        try:
            user_id = int(parts[2])
            offset = max(0, min(10000, int(parts[3])))
            users_offset = max(0, min(10000, int(parts[4])))
        except (IndexError, ValueError):
            await query.answer(tr(language, "admin_invalid_user"), show_alert=True)
            return
        rows = storage.history_for_user(user_id, offset, 9)
        label = f"@{rows[0]['username']}" if rows and rows[0]["username"] else str(user_id)
        lines = [tr(language, "admin_history_user", label=html.escape(label), user_id=user_id)]
        for row in rows[:8]:
            when = datetime.datetime.fromtimestamp(row["created_at"], datetime.timezone.utc).strftime("%d.%m %H:%M UTC")
            body = (row["body"] or f"[{row['message_type']}]").replace("\n", " ")[:180]
            lines.append(f"{when} · {html.escape(body)}")
        if not rows:
            lines.append(tr(language, "admin_no_messages"))
        controls = []
        if offset:
            controls.append((tr(language, "admin_newer"), f"adm:history_user:{user_id}:{max(0, offset - 8)}:{users_offset}"))
        if len(rows) > 8:
            controls.append((tr(language, "admin_older"), f"adm:history_user:{user_id}:{offset + 8}:{users_offset}"))
        buttons = [controls] if controls else []
        buttons.append([(tr(language, "admin_users"), f"adm:history:{users_offset}")])
        await query.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard(buttons),
                                      disable_web_page_preview=True)
    elif action == "files":
        now = int(time.time())
        if len(parts) < 4:
            try:
                offset = max(0, min(1000, int(parts[2])))
            except (IndexError, ValueError):
                offset = 0
            dates = storage.db.execute(
                "SELECT date(ready_at, 'unixepoch') AS day, count(*) AS count FROM jobs "
                "WHERE file_path IS NOT NULL AND link_expires_at>? AND ready_at IS NOT NULL "
                "GROUP BY day ORDER BY day DESC LIMIT 11 OFFSET ?", (now, offset),
            ).fetchall()
            buttons = [[(f"📁 {row['day']} · {row['count']}", f"adm:files:{row['day']}:0")]
                       for row in dates[:10]]
            navigation = []
            if offset:
                navigation.append((tr(language, "admin_newer"), f"adm:files:{max(0, offset - 10)}"))
            if len(dates) > 10:
                navigation.append((tr(language, "admin_older"), f"adm:files:{offset + 10}"))
            if navigation:
                buttons.append(navigation)
            buttons.append([(tr(language, "admin_back"), "adm:back")])
            await query.message.edit_text(
                tr(language, "admin_files_by_date") if dates else tr(language, "admin_files_empty"), reply_markup=keyboard(buttons))
        else:
            day = parts[2]
            try:
                datetime.date.fromisoformat(day)
                offset = max(0, min(1000, int(parts[3])))
            except ValueError:
                await query.answer(tr(language, "admin_invalid_date"), show_alert=True)
                return
            rows = storage.db.execute(
                "SELECT * FROM jobs WHERE file_path IS NOT NULL AND token IS NOT NULL "
                "AND link_expires_at>? AND date(ready_at, 'unixepoch')=? "
                "ORDER BY ready_at DESC LIMIT 11 OFFSET ?", (now, day, offset),
            ).fetchall()
            lines = [tr(language, "admin_files_date_heading", day=day)]
            shown = 0
            for row in rows[:10]:
                if not Path(row["file_path"]).is_file() or not PUBLIC_BASE_URL:
                    continue
                link = html.escape(PUBLIC_BASE_URL + public_path(row), quote=True)
                title = html.escape(row["title"][:90])
                lines.append(f"• <a href=\"{link}\">{title}</a> · {size_label(row['file_size'] or 0)}")
                shown += 1
            if not shown:
                lines.append(tr(language, "admin_files_none_available"))
            navigation = []
            if offset:
                navigation.append((tr(language, "admin_newer"), f"adm:files:{day}:{max(0, offset - 10)}"))
            if len(rows) > 10:
                navigation.append((tr(language, "admin_older"), f"adm:files:{day}:{offset + 10}"))
            buttons = [navigation] if navigation else []
            buttons.append([(tr(language, "admin_dates"), "adm:files:0")])
            await query.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=keyboard(buttons),
                                          disable_web_page_preview=True)
    elif action == "clean":
        temp_root = DATA_DIR / "jobs"
        folders = sum(1 for entry in temp_root.iterdir() if entry.is_dir()) if temp_root.exists() else 0
        bytes_on_disk = sum(entry.lstat().st_size for entry in temp_root.rglob("*")
                            if entry.is_file() and not entry.is_symlink()) if temp_root.exists() else 0
        await query.message.edit_text(
            tr(language, "admin_clean_prompt", folders=folders, size=size_label(bytes_on_disk)),
            reply_markup=keyboard([[(tr(language, 'admin_clean_delete'), 'adm:clean_confirm')],
                                  [(tr(language, 'admin_back'), 'adm:back')]]),
        )
    elif action == "clean_confirm":
        active = storage.db.execute("SELECT count(*) FROM jobs WHERE status IN ('downloading','processing','uploading')").fetchone()[0]
        if current_job or active:
            await query.answer(tr(language, "admin_clean_busy"), show_alert=True)
            return
        temp_root = DATA_DIR / "jobs"
        folders = sum(1 for entry in temp_root.iterdir() if entry.is_dir()) if temp_root.exists() else 0
        free_before = shutil.disk_usage(DATA_DIR).free
        try:
            if temp_root.exists():
                shutil.rmtree(temp_root)
            temp_root.mkdir(parents=True, exist_ok=True)
            temp_root.chmod(0o700)
        except OSError:
            await query.answer(tr(language, "admin_clean_failed"), show_alert=True)
            return
        count = storage.remove_temp_files()
        freed = max(0, shutil.disk_usage(DATA_DIR).free - free_before)
        await query.message.edit_text(
            tr(language, "admin_clean_done", folders=folders, freed=size_label(freed), links=count),
            reply_markup=keyboard([[(tr(language, 'admin_back'), 'adm:back')]]),
        )
    await query.answer()


@router.message(Command("cancel"))
async def cancel_command(message: Message) -> None:
    if not message.from_user:
        return
    prompt = storage.pop_prompt(message.chat.id)
    if prompt:
        options = json.loads(prompt["options"] or "{}")
        requester_id = int(options.get("requester_id") or message.chat.id)
        if requester_id == message.from_user.id and prompt["action"] in ("clip_range", "audio_edit_range"):
            language = storage.settings(requester_id)["language"]
            await message.answer(tr(language, "clip_cancelled"))
            return
        storage.set_prompt(message.chat.id, prompt["action"], prompt["job_id"], options)
    if not is_owner(message.from_user.id):
        return
    if current_job:
        cancelled_jobs.add(current_job)
        await message.answer("Отменяю текущую задачу.")
    else:
        await message.answer("Сейчас нет задачи в работе.")


@router.message(F.photo | F.document)
async def receive_bug_report_media(message: Message) -> None:
    if (message.chat.type != "private" or not message.from_user
            or not is_owner(message.from_user.id)):
        return
    caption = message.caption or ""
    if not BUG_REPORT_TAG.search(caption):
        return
    language = storage.settings(message.from_user.id)["language"]
    description = BUG_REPORT_TAG.sub("", caption, count=1).strip(" \n\t:—-")
    if not description:
        await message.answer(tr(language, "bug_need_description"))
        return
    if message.photo:
        attachment = message.photo[-1]
        screenshot_name = "screenshot.jpg"
    elif message.document and message.document.mime_type in {
            "image/jpeg", "image/png", "image/webp"}:
        attachment = message.document
        screenshot_name = {"image/jpeg": "screenshot.jpg", "image/png": "screenshot.png",
                           "image/webp": "screenshot.webp"}[attachment.mime_type]
    else:
        await message.answer(tr(language, "bug_need_image"))
        return
    if attachment.file_size and attachment.file_size > BUG_REPORT_MAX_BYTES:
        await message.answer(tr(language, "bug_image_too_large"))
        return
    ticket_id = None
    try:
        report = bug_reports.create_pending(description, chat_id=message.chat.id,
                                            message_id=message.message_id)
        ticket_id = report["number"]
        screenshot = bug_reports.folder(ticket_id) / screenshot_name
        await bot.download(attachment.file_id, destination=screenshot)
        if not 0 < screenshot.stat().st_size <= BUG_REPORT_MAX_BYTES:
            raise ValueError("Invalid screenshot size")
        screenshot.chmod(0o600)
        bug_reports.finalize(ticket_id, screenshot_name)
    except Exception as exc:
        if ticket_id is not None:
            try:
                bug_reports.discard_pending(ticket_id)
            except OSError:
                log.error("Could not remove incomplete bug report %s", ticket_id)
        log.error("Could not save bug report: %s", type(exc).__name__)
        await message.answer(tr(language, "bug_save_failed"))
        return
    await message.answer(tr(language, "bug_saved", ticket=report["ticket"]))


@router.message(F.text)
async def receive_url(message: Message) -> None:
    if (message.chat.type in ("group", "supergroup") and message.from_user
            and is_owner(message.from_user.id) and not storage.group_enabled(message.chat.id)):
        storage.enable_group(message.chat.id)
    if not message.from_user or not chat_allowed(message.chat.id, message.from_user.id):
        return
    raw = (message.text or "").strip()
    if message.chat.type == "private" and is_owner(message.from_user.id) and BUG_REPORT_TAG.search(raw):
        language = storage.settings(message.from_user.id)["language"]
        await message.answer(tr(language, "bug_need_image"))
        return
    command_token = raw.split(maxsplit=1)[0].split("@", 1)[0].lower() if raw.startswith("/") else ""
    is_fresh_download = raw.startswith(("http://", "https://")) or command_token in {"/dd", "/download"}
    pending = storage.pop_prompt(message.chat.id)
    # A new URL or /dd command begins a new download instead of answering a stale prompt.
    if is_fresh_download:
        pending = None
    if pending:
        options = json.loads(pending["options"] or "{}")
        requester_id = int(options.get("requester_id") or message.chat.id)
        if requester_id != message.from_user.id:
            storage.set_prompt(message.chat.id, pending["action"], pending["job_id"], options)
            return
        language = storage.settings(requester_id)["language"]
        text = (message.text or "").strip()
        if text.lower() in {"отмена", "cancel"}:
            await message.answer(tr(language, "clip_cancelled"))
            return
        if pending["action"] == "clip_range":
            try:
                start, end = parse_clip_range(text)
                source = storage.get(pending["job_id"])
                if not source or source["status"] != "awaiting_selection":
                    await message.answer(tr(language, "clip_source_expired"))
                    return
                source_info = json.loads(source["metadata"])
                duration = source_info.get("duration")
                if duration and end > int(float(duration)):
                    storage.set_prompt(message.chat.id, pending["action"], pending["job_id"], options)
                    await message.answer(tr(language, "clip_exceeds_duration").format(
                        duration=int(float(duration))))
                    return
                if storage.queued_count() >= MAX_QUEUE_LENGTH:
                    storage.set_prompt(message.chat.id, pending["action"], pending["job_id"], options)
                    await message.answer(tr(language, "queue_full"))
                    return
            except ValueError:
                storage.set_prompt(message.chat.id, pending["action"], pending["job_id"], options)
                await message.answer(tr(language, "clip_bad_range"))
                return
            clip_info = dict(source_info)
            clip_info["_clip_range"] = [start, end]
            clip_info["duration"] = end - start
            title = f"{source['title']} [{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}]"
            new_id = storage.add_job(source["owner_id"], source["url"], title, clip_info,
                                     options["selection"], message.message_id,
                                     requester_id=requester_id)
            position = storage.db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
            await change_status(new_id, tr(language, "clip_queued").format(position=position), force=True)
            queue_event.set()
            return
        if pending["action"] == "audio_edit_range":
            try:
                source = storage.get(pending["job_id"])
                if (not source or source["status"] not in ("sent", "link_only")
                        or source["owner_id"] != message.chat.id
                        or (source["requester_id"] or source["owner_id"]) != requester_id
                        or not source["file_path"] or not Path(source["file_path"]).is_file()):
                    await message.answer("Исходный аудиофайл больше не хранится на сервере.")
                    return
                if text.lower() in {"весь", "whole", "all"}:
                    start, end = 0, 0
                else:
                    start, end = parse_clip_range(text)
                    duration = json.loads(source["metadata"]).get("duration")
                    if duration and end > int(float(duration)) + 1:
                        raise ValueError("Фрагмент длиннее исходного файла.")
                if storage.queued_count() >= MAX_QUEUE_LENGTH:
                    await message.answer(tr(language, "queue_full"))
                    return
            except ValueError:
                storage.set_prompt(message.chat.id, pending["action"], pending["job_id"], options)
                await message.answer(tr(language, "audio_edit_enter_range"),
                                     reply_markup=audio_edit_cancel_buttons(pending["job_id"], language))
                return
            edit_info = json.loads(source["metadata"])
            edit_info.pop("_source_request_message_id", None)
            edit_info["_audio_edit"] = {"source_job_id": source["id"], "codec": options["codec"],
                                        "bitrate": options["bitrate"], "start": start, "end": end}
            if end:
                edit_info["duration"] = end - start
            output_title = audio_output_title(source, edit_info)
            new_id = storage.add_job(source["owner_id"], source["url"], output_title, edit_info,
                                     f"audio:{options['codec']}", message.message_id,
                                     requester_id=requester_id)
            await change_status(new_id, f"{output_title}\n\nВ очереди: обработка аудио", force=True)
            queue_event.set()
            return
        storage.set_prompt(message.chat.id, pending["action"], pending["job_id"], options)
    if disk_critical():
        await message.answer(unavailable_text(message.chat.id))
        return
    request_command = raw.split(maxsplit=1)[0].split("@", 1)[0].lower() if raw.startswith("/") else ""
    if raw.startswith("/"):
        command, *arguments = raw.split(maxsplit=1)
        name, _, mention = command[1:].partition("@")
        if name.lower() not in ("dd", "download") or (mention and mention.lower() != BOT_USERNAME.lower()):
            return
        raw = arguments[0].strip() if arguments else ""
        if not raw:
            await message.answer("Укажите ссылку после команды: /dd https://…")
            return
    elif message.chat.type in ("group", "supergroup"):
        return
    try:
        url = normalize_media_url(raw)
    except MediaError as exc:
        if message.chat.type == "private" or raw.startswith(("http://", "https://")):
            await message.answer(str(exc))
        return
    url_error = validate_platform_url(url)
    if url_error:
        await message.answer(url_error)
        return
    if message.chat.type in ("group", "supergroup") and storage.queued_count() >= MAX_QUEUE_LENGTH:
        await message.answer("Очередь заполнена. Попробуйте позже.")
        return
    waiting = await message.answer("Ссылка получена. Ожидает обработки…")
    try:
        language = storage.settings(message.from_user.id)["language"]
        info = await fetch_metadata(url, waiting, language)
    except MediaError as exc:
        await waiting.edit_text(str(exc))
        if exc.code == "yandex_not_configured" and OWNER_ID:
            requester = message.from_user
            requester_name = " ".join(part for part in (requester.first_name, requester.last_name) if part)
            requester_label = f"{requester_name} (@{requester.username})" if requester.username else requester_name
            requester_label = requester_label or "пользователь без имени"
            safe_url = f"{urlparse(url).scheme}://{urlparse(url).netloc}{urlparse(url).path}"
            try:
                await bot.send_message(
                    OWNER_ID,
                    "Запрос Яндекс Музыки без настроенного токена.\n"
                    f"Пользователь: {requester_label} · ID {requester.id}\n"
                    f"Чат: {message.chat.title or message.chat.id}\n"
                    f"Ссылка: {safe_url}\n\n"
                    "Откройте /admin → Яндекс Музыка, чтобы подключить или обновить аккаунт.",
                    disable_web_page_preview=True,
                )
            except Exception as notify_error:
                log.error("Could not notify owner about unconfigured Yandex Music: %s",
                          type(notify_error).__name__)
        return
    except Exception as exc:
        log.warning("Metadata failed for %s: %s: %s", _safe_log_url(url),
                    type(exc).__name__, _safe_log_detail(str(exc)))
        await waiting.edit_text("Не удалось получить данные. Проверьте ссылку и доступность ролика.")
        return
    if message.chat.type in ("group", "supergroup"):
        settings = storage.settings(message.from_user.id)
        if request_command in {"/dd", "/download"}:
            info = dict(info)
            info["_source_request_message_id"] = message.message_id
        job_id = storage.add_card(message.chat.id, url, info["title"], info,
                                  requester_id=message.from_user.id)
        is_photo = False
        try:
            if info.get("thumbnail"):
                card = await message.answer_photo(
                    info["thumbnail"], caption=card_text(info), parse_mode="HTML",
                    reply_markup=card_buttons(job_id, info, settings["language"]), request_timeout=15)
                is_photo = True
            else:
                card = await message.answer(
                    card_text(info), parse_mode="HTML",
                    reply_markup=card_buttons(job_id, info, settings["language"]))
        except Exception:
            card = await message.answer(
                card_text(info), parse_mode="HTML",
                reply_markup=card_buttons(job_id, info, settings["language"]))
        storage.update(job_id, status_message_id=card.message_id, status_is_photo=int(is_photo))
        await waiting.delete()
        return
    settings = storage.settings(message.from_user.id)
    heights = offered_heights(info)
    if settings["auto_download"] and len(heights) == 1:
        selection = f"video:{heights[0]}" + (":portrait" if is_portrait(info) else "")
        estimate = estimated_size(info, selection)
        if (estimate is not None and estimate <= 20 * 1024**2
                and estimate <= available_file_limit(shutil.disk_usage(DATA_DIR).free)
                and storage.queued_count() < MAX_QUEUE_LENGTH):
            job_id = storage.add_job(message.chat.id, url, info["title"], info, selection,
                                     waiting.message_id, requester_id=message.from_user.id)
            await change_status(job_id, "В очереди", force=True)
            queue_event.set()
            return
    job_id = storage.add_card(message.chat.id, url, info["title"], info,
                              requester_id=message.from_user.id)
    is_photo = False
    try:
        if info.get("thumbnail"):
            card = await message.answer_photo(info["thumbnail"], caption=card_text(info),
                                              parse_mode="HTML", reply_markup=card_buttons(job_id, info, settings["language"]),
                                              request_timeout=15)
            is_photo = True
        else:
            card = await message.answer(card_text(info), parse_mode="HTML", reply_markup=card_buttons(job_id, info, settings["language"]))
    except Exception:
        card = await message.answer(card_text(info), parse_mode="HTML", reply_markup=card_buttons(job_id, info, settings["language"]))
    storage.update(job_id, status_message_id=card.message_id, status_is_photo=int(is_photo))
    await waiting.delete()


@router.callback_query(F.data.startswith("sel:"))
async def select_format(query: CallbackQuery) -> None:
    if disk_critical():
        await query.answer(unavailable_text(query.message.chat.id if query.message else 0), show_alert=True)
        return
    try:
        _, job_id, choice = (query.data or "").split(":", 2)
        row = storage.get(job_id)
        if not callback_allowed(query, row) or row["status"] != "awaiting_selection":
            raise ValueError
        info = json.loads(row["metadata"])
        if query.message.photo:
            info["card_photo_file_id"] = query.message.photo[-1].file_id
        if choice.startswith("v") and int(choice[1:]) in offered_heights(info):
            selection = f"video:{int(choice[1:])}" + (":portrait" if is_portrait(info) else "")
        elif choice == "vbest":
            selection = "video:best"
        elif choice in ("am4a", "amp3"):
            selection = "audio:" + choice[1:]
        else:
            raise ValueError
    except (ValueError, TypeError):
        await query.answer("Этот вариант больше недоступен.", show_alert=True)
        return
    if storage.queued_count() >= MAX_QUEUE_LENGTH:
        await query.answer("Очередь заполнена. Попробуйте позже.", show_alert=True)
        return
    estimate = estimated_size(info, selection)
    limit = available_file_limit(shutil.disk_usage(DATA_DIR).free)
    if estimate is not None and estimate > limit:
        await query.answer(capacity_message(estimate, limit), show_alert=True)
        return
    new_id = storage.add_job(row["owner_id"], row["url"], row["title"], info, selection,
                             query.message.message_id, requester_id=row["requester_id"])
    storage.update(new_id, status_is_photo=int(bool(query.message.photo)))
    await query.answer()
    position = storage.db.execute("SELECT count(*) FROM jobs WHERE status='queued'").fetchone()[0]
    await change_status(new_id, f"{row['title']}\n\nВ очереди: позиция {position}\n{progress_bar(0, None)}", force=True)
    queue_event.set()


@router.callback_query(F.data.startswith("clip:"))
async def choose_clip_format(query: CallbackQuery) -> None:
    try:
        job_id = (query.data or "").split(":", 1)[1]
        row = storage.get(job_id)
        if (not callback_allowed(query, row) or row["status"] != "awaiting_selection"
                or row["requester_id"] != query.from_user.id):
            raise ValueError
        info = json.loads(row["metadata"])
    except (ValueError, TypeError):
        await query.answer("Вариант больше недоступен", show_alert=True)
        return
    language = storage.settings(query.from_user.id)["language"]
    await query.answer()
    await query.message.answer(tr(language, "clip_choose_format"),
                               reply_markup=clip_format_buttons(job_id, info, language))


@router.callback_query(F.data.startswith("clipcancel:"))
async def cancel_clip_prompt(query: CallbackQuery) -> None:
    job_id = (query.data or "").split(":", 1)[1]
    row = storage.get(job_id)
    if (not callback_allowed(query, row) or row["requester_id"] != query.from_user.id):
        await query.answer("Вариант больше недоступен", show_alert=True)
        return
    storage.pop_prompt(query.message.chat.id)
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)


@router.callback_query(F.data.startswith("clipfmt:"))
async def request_clip_range(query: CallbackQuery) -> None:
    try:
        _, job_id, choice = (query.data or "").split(":", 2)
        row = storage.get(job_id)
        if (not callback_allowed(query, row) or row["status"] != "awaiting_selection"
                or row["requester_id"] != query.from_user.id):
            raise ValueError
        info = json.loads(row["metadata"])
        if choice.startswith("v") and choice != "vbest" and int(choice[1:]) in offered_heights(info):
            selection = f"video:{int(choice[1:])}" + (":portrait" if is_portrait(info) else "")
        elif choice == "vbest" and any(f.get("vcodec") not in (None, "none") for f in info.get("formats", [])):
            selection = "video:best"
        else:
            raise ValueError
    except (ValueError, TypeError):
        await query.answer("Вариант больше недоступен", show_alert=True)
        return
    language = storage.settings(query.from_user.id)["language"]
    storage.set_prompt(query.message.chat.id, "clip_range", job_id,
                       {"requester_id": query.from_user.id, "selection": selection})
    await query.answer()
    await query.message.answer(tr(language, "clip_enter_range").format(max_minutes=MAX_CLIP_SECONDS // 60))


@router.callback_query(F.data.startswith("cancel:"))
async def cancel_button(query: CallbackQuery) -> None:
    job_id = (query.data or "").split(":", 1)[1]
    row = storage.get(job_id)
    if callback_allowed(query, row) and row["status"] in ("queued", "downloading", "processing", "uploading"):
        cancelled_jobs.add(job_id)
        if row["status"] == "queued":
            storage.update(job_id, status="cancelled")
            cancelled_jobs.discard(job_id)
            last_progress.pop(job_id, None)
        await query.answer("Отменяю")
    else:
        await query.answer("Задача уже завершена")


@router.callback_query(F.data.startswith("again:"))
async def again_button(query: CallbackQuery) -> None:
    if disk_critical():
        await query.answer(unavailable_text(query.message.chat.id if query.message else 0), show_alert=True)
        return
    row = storage.get((query.data or "").split(":", 1)[1])
    if not callback_allowed(query, row):
        await query.answer("Задача не найдена")
        return
    info = json.loads(row["metadata"])
    language = storage.settings(row["requester_id"] or row["owner_id"])["language"]
    card_id = storage.add_card(row["owner_id"], row["url"], row["title"], info,
                               requester_id=row["requester_id"])
    is_photo = False
    try:
        if info.get("thumbnail"):
            card = await bot.send_photo(row["owner_id"], info["thumbnail"], caption=card_text(info),
                                        parse_mode="HTML", reply_markup=card_buttons(card_id, info, language),
                                        request_timeout=15)
            is_photo = True
        else:
            card = await bot.send_message(row["owner_id"], card_text(info),
                                          parse_mode="HTML", reply_markup=card_buttons(card_id, info, language))
    except Exception:
        card = await bot.send_message(row["owner_id"], card_text(info),
                                      parse_mode="HTML", reply_markup=card_buttons(card_id, info, language))
    storage.update(card_id, status_message_id=card.message_id, status_is_photo=int(is_photo))
    await query.answer()


@router.callback_query(F.data.startswith("audio:"))
async def audio_button(query: CallbackQuery) -> None:
    if disk_critical():
        await query.answer(unavailable_text(query.message.chat.id if query.message else 0), show_alert=True)
        return
    row = storage.get((query.data or "").split(":", 1)[1])
    if not callback_allowed(query, row):
        await query.answer("Задача не найдена")
        return
    if storage.queued_count() >= MAX_QUEUE_LENGTH:
        await query.answer("Очередь заполнена. Попробуйте позже.", show_alert=True)
        return
    new_id = storage.add_job(row["owner_id"], row["url"], row["title"], json.loads(row["metadata"]),
                             "audio:m4a", 0, requester_id=row["requester_id"])
    await change_status(new_id, "Извлекаю аудио…", force=True)
    queue_event.set()
    await query.answer("Добавлено в очередь")


@router.callback_query(F.data.startswith("audioedit:"))
async def audio_edit_button(query: CallbackQuery) -> None:
    row = storage.get((query.data or "").split(":", 1)[1])
    if (not callback_allowed(query, row) or not row["selection"].startswith("audio:")
            or (row["requester_id"] or row["owner_id"]) != query.from_user.id
            or row["status"] not in ("sent", "link_only") or not row["file_path"]
            or not Path(row["file_path"]).is_file()):
        await query.answer("Исходный аудиофайл больше не хранится на сервере.", show_alert=True)
        return
    job_id = row["id"]
    await query.message.answer("Выберите формат и битрейт:", reply_markup=keyboard([
        [("MP3 128 kbps", f"aefmt:{job_id}:mp3:128"),
         ("MP3 192 kbps", f"aefmt:{job_id}:mp3:192")],
        [("MP3 320 kbps", f"aefmt:{job_id}:mp3:320"),
         ("M4A 128 kbps", f"aefmt:{job_id}:m4a:128")],
        [("M4A 192 kbps", f"aefmt:{job_id}:m4a:192"),
         ("M4A 256 kbps", f"aefmt:{job_id}:m4a:256")],
    ]))
    await query.answer()


@router.callback_query(F.data.startswith("aefmt:"))
async def audio_edit_format(query: CallbackQuery) -> None:
    try:
        _, job_id, codec, bitrate_text = (query.data or "").split(":", 3)
        bitrate = int(bitrate_text)
        row = storage.get(job_id)
        if (not callback_allowed(query, row) or not row["selection"].startswith("audio:")
                or (row["requester_id"] or row["owner_id"]) != query.from_user.id
                or row["status"] not in ("sent", "link_only") or not row["file_path"]
                or not Path(row["file_path"]).is_file() or codec not in ("mp3", "m4a")
                or bitrate not in (128, 192, 256, 320)):
            raise ValueError
    except (TypeError, ValueError):
        await query.answer("Исходный файл или вариант больше недоступен.", show_alert=True)
        return
    storage.set_prompt(query.message.chat.id, "audio_edit_range", job_id,
                       {"requester_id": query.from_user.id, "codec": codec, "bitrate": bitrate})
    language = storage.settings(query.from_user.id)["language"]
    await query.message.answer(tr(language, "audio_edit_enter_range"),
                               reply_markup=audio_edit_cancel_buttons(job_id, language))
    await query.answer()


def audio_edit_cancel_buttons(job_id: str, language: str) -> InlineKeyboardMarkup:
    return keyboard([[(tr(language, "clip_cancel_button"), f'audioeditcancel:{job_id}')]])


@router.callback_query(F.data.startswith("audioeditcancel:"))
async def cancel_audio_edit_prompt(query: CallbackQuery) -> None:
    job_id = (query.data or "").split(":", 1)[1]
    row = storage.get(job_id)
    if (not callback_allowed(query, row)
            or (row["requester_id"] or row["owner_id"]) != query.from_user.id):
        await query.answer("Запрос больше недоступен", show_alert=True)
        return
    prompt = storage.pop_prompt(query.message.chat.id)
    if prompt and prompt["action"] == "audio_edit_range" and prompt["job_id"] == job_id:
        language = storage.settings(query.from_user.id)["language"]
        await query.message.edit_text(tr(language, "audio_edit_cancelled"))
        await query.answer(tr(language, "clip_cancelled"))
        return
    if prompt:
        storage.set_prompt(query.message.chat.id, prompt["action"], prompt["job_id"],
                           json.loads(prompt["options"] or "{}"))
    await query.answer("Запрос уже завершён", show_alert=True)


async def run_download_worker(row, job_dir: Path, job_id: str) -> Path:
    available = shutil.disk_usage(DATA_DIR).free
    max_file = available_file_limit(available)
    max_temp = min(MAX_JOB_BYTES * 3, max(0, available - MIN_FREE_BYTES))
    requester_id = row["requester_id"] or row["owner_id"]
    apple_compatible = bool(storage.settings(requester_id)["apple_compatible"])
    metadata = json.loads(row["metadata"])
    audio_edit = metadata.get("_audio_edit")
    clip_range = metadata.get("_clip_range")
    clip_args = [str(int(clip_range[0])), str(int(clip_range[1]))] if clip_range else []
    async def execute(command: list[str], output_root: Path) -> Path:
        with worker_error_stream(DATA_DIR) as error_stream:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=error_stream,
                start_new_session=True, limit=8192,
            )
        deadline = time.monotonic() + 14400
        final_path: Path | None = None
        try:
            while True:
                if job_id in cancelled_jobs:
                    raise MediaError("Загрузка отменена.")
                if time.monotonic() >= deadline:
                    raise MediaError("Обработка заняла слишком много времени.")
                if process_group_rss(process.pid) > MAX_WORKER_RSS_BYTES:
                    raise MediaError("Обработка превысила лимит памяти сервера.")
                if shutil.disk_usage(DATA_DIR).free < MIN_FREE_BYTES:
                    raise MediaError("Недостаточно свободного места на сервере.")
                if job_dir.exists():
                    temp_bytes = sum(path.lstat().st_size for path in job_dir.rglob("*")
                                     if path.is_file() and not path.is_symlink())
                    if temp_bytes > max_temp:
                        raise MediaError("Временные файлы превысили лимит задачи.")
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                if not line:
                    break
                if len(line) > 4096:
                    raise MediaError("Источник вернул слишком большой ответ.")
                try:
                    event = json.loads(line)
                except ValueError as exc:
                    log.error("Worker %s sent non-JSON stdout: %r", job_id, line[:160])
                    raise MediaError("Источник вернул некорректные данные.") from exc
                if event.get("type") == "progress":
                    amount = int(event.get("downloaded") or 0)
                    total = event.get("total")
                    progress_state[job_id].update(downloaded=amount, total=total)
                    detail = f"{amount / 1024**2:.1f} МБ"
                    if total:
                        detail += f" / {int(total) / 1024**2:.1f} МБ"
                    language = job_language(row)
                    await change_status(
                        job_id,
                        f"{row['title']}\n\n{selected_format(row, language)}\n"
                        f"{tr(language, 'download_stage')}\n{progress_bar(amount, total)}\n{detail}",
                    )
                elif event.get("type") == "error":
                    raise MediaError(str(event.get("message") or "Не удалось обработать файл.")[:250])
                elif event.get("type") == "result":
                    final_path = Path(str(event.get("path") or ""))
            await process.wait()
            if process.returncode != 0 or final_path is None:
                raise MediaError("Не удалось скачать или подготовить файл.")
            if not final_path.is_file() or not final_path.resolve().is_relative_to(output_root.resolve()):
                raise MediaError("Готовый файл не найден.")
            return final_path
        except BaseException:
            await terminate_process_group(process)
            raise

    if audio_edit:
        source = storage.get(audio_edit["source_job_id"])
        source_path = Path(source["file_path"]) if source and source["file_path"] else None
        if (not source or source["owner_id"] != row["owner_id"] or not source_path
                or not source_path.is_file() or not source_path.resolve().is_relative_to((DATA_DIR / "jobs").resolve())):
            raise MediaError("Исходный аудиофайл больше не хранится на сервере.")
        command = [sys.executable, "-m", "media_bot.workers.audio_edit", str(source_path), str(job_dir),
                   str(audio_edit["codec"]), str(audio_edit["bitrate"]),
                   str(audio_edit["start"]), str(audio_edit["end"])]
    else:
        command = [sys.executable, "-m", "media_bot.workers.download", row["url"], row["selection"],
                   str(job_dir), str(max_file), str(MIN_FREE_BYTES), str(max_temp),
                   "1" if apple_compatible else "0", *clip_args]
    return await execute(command, job_dir)


async def process_job(job_id: str) -> None:
    row = storage.get(job_id)
    if not row or row["status"] != "queued":
        return
    if disk_critical():
        raise MediaError(unavailable_text(row["owner_id"]))
    job_dir = DATA_DIR / "jobs" / job_id
    if shutil.disk_usage(DATA_DIR).free < MIN_FREE_BYTES + 100_000_000:
        raise MediaError("Недостаточно свободного места на сервере.")
    estimate = estimated_size(json.loads(row["metadata"]), row["selection"])
    limit = available_file_limit(shutil.disk_usage(DATA_DIR).free)
    if estimate is not None and estimate > limit:
        raise MediaError(capacity_message(estimate, limit))
    is_edit = bool(json.loads(row["metadata"]).get("_audio_edit"))
    stage = "processing" if is_edit else "downloading"
    language = job_language(row)
    storage.update(job_id, status=stage)
    progress_state[job_id] = {"stage": stage, "started": time.monotonic(), "downloaded": 0, "total": None}
    if is_edit:
        initial_status = tr(language, "processing_audio")
    else:
        initial_status = (f"{selected_format(row, language)}\n{tr(language, 'download_stage')}\n"
                          f"{tr(language, 'download_connecting')}")
    await change_status(job_id, f"{row['title']}\n\n{initial_status}", force=True)
    async with heavy_job_lock:
        if job_id in cancelled_jobs:
            raise MediaError("Загрузка отменена.")
        path = await run_download_worker(row, job_dir, job_id)
    is_audio = row["selection"].startswith("audio:")
    if is_audio:
        target = path.with_name(audio_filename(
            audio_output_title(row, json.loads(row["metadata"])), path.suffix))
        if target != path:
            if target.exists():
                raise MediaError("Не удалось безопасно назвать готовый аудиофайл.")
            path.rename(target)
            path = target
    if job_id in cancelled_jobs:
        raise MediaError("Загрузка отменена.")
    now = int(time.time())
    token = secrets.token_hex(16)
    storage.update(job_id, status="uploading", file_path=str(path), file_size=path.stat().st_size,
                   token=token, ready_at=now, link_expires_at=now + LINK_TTL, delete_after=now + LINK_TTL)
    progress_state[job_id].update(stage="uploading", started=time.monotonic(), uploaded=0, total=path.stat().st_size)
    url = PUBLIC_BASE_URL + public_path(storage.get(job_id)) if PUBLIC_BASE_URL else ""
    status = "Файл готов. Загружаю в Telegram…"
    upload_limit = TELEGRAM_FILE_LIMIT if LOCAL_API_URL else 50_000_000
    if path.stat().st_size > upload_limit:
        storage.update(job_id, status="link_only")
        await change_status(job_id, status + "\nФайл превышает предел отправки через Telegram.", force=True, link_url=url)
        return
    await change_status(job_id, status, force=True, link_url=url)
    caption, description_rest = result_caption(
        row, url, path.stat().st_size,
        include_server_link=is_owner(row["requester_id"] or row["owner_id"]))
    media = ProgressInputFile(path, job_id)
    if is_audio:
        message = await bot.send_audio(row["owner_id"], media, caption=caption, parse_mode="HTML",
                                       reply_markup=result_buttons(job_id, True), request_timeout=7200)
        file_id = message.audio.file_id if message.audio else None
    else:
        video_info = await asyncio.to_thread(subprocess.run,
            ["ffprobe", "-v", "error", "-show_entries", "stream=width,height:format=duration",
             "-of", "json", str(path)], capture_output=True, text=True, check=True, timeout=30)
        probed = json.loads(video_info.stdout)
        video_stream = next((stream for stream in probed.get("streams", []) if stream.get("width")), {})
        duration = round(float(probed.get("format", {}).get("duration") or 0))
        thumb = job_dir / "preview.jpg"
        photo_id = json.loads(row["metadata"]).get("card_photo_file_id")
        source_cover = job_dir / "cover-source"
        if photo_id:
            try:
                await bot.download(photo_id, destination=source_cover)
                await asyncio.to_thread(subprocess.run,
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_cover),
                     "-frames:v", "1", "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                     "-q:v", "5", str(thumb)], capture_output=True, timeout=60, check=True)
            except Exception as exc:
                log.warning("Could not use card cover for %s: %s", job_id, type(exc).__name__)
            finally:
                source_cover.unlink(missing_ok=True)
        if not thumb.is_file():
            await asyncio.to_thread(subprocess.run,
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "2", "-i", str(path),
                 "-frames:v", "1", "-vf", "scale=320:320:force_original_aspect_ratio=decrease",
                 "-q:v", "5", str(thumb)], capture_output=True, timeout=60)
        thumb_file = FSInputFile(thumb) if thumb.is_file() and thumb.stat().st_size < 200_000 else None
        try:
            message = await bot.send_video(row["owner_id"], media, caption=caption, supports_streaming=True,
                                           parse_mode="HTML", duration=duration,
                                           width=video_stream.get("width"), height=video_stream.get("height"),
                                           thumbnail=thumb_file,
                                           reply_markup=result_buttons(job_id, False), request_timeout=7200)
            file_id = message.video.file_id if message.video else None
        except Exception as exc:
            log.warning("Video send failed for %s: %s", job_id, type(exc).__name__)
            progress_state[job_id]["uploaded"] = 0
            message = await bot.send_document(row["owner_id"], ProgressInputFile(path, job_id), caption=caption,
                                              parse_mode="HTML", reply_markup=result_buttons(job_id, False), request_timeout=7200)
            file_id = message.document.file_id if message.document else None
    uploaded = int(time.time())
    storage.update(job_id, status="sent", file_id=file_id, result_message_id=message.message_id,
                   uploaded_at=uploaded, delete_after=uploaded + LINK_TTL)
    try:
        source_request_id = json.loads(row["metadata"]).get("_source_request_message_id")
        if row["owner_id"] < 0 and isinstance(source_request_id, int):
            await bot.delete_message(row["owner_id"], source_request_id)
    except Exception as exc:
        log.warning("Could not remove group download request for %s: %s", job_id, type(exc).__name__)
    if description_rest:
        try:
            await send_description_continuation(row["owner_id"], description_rest, message.message_id)
        except Exception as exc:
            log.warning("Could not send description for %s: %s", job_id, type(exc).__name__)
    status_message_id = storage.get(job_id)["status_message_id"]
    if status_message_id:
        try:
            await bot.delete_message(row["owner_id"], status_message_id)
            storage.update(job_id, status_message_id=None)
        except Exception as exc:
            log.warning("Could not remove progress message for %s: %s", job_id, type(exc).__name__)
            await change_status(job_id, "Готово. Файл отправлен в Telegram.", force=True)


async def status_heartbeat(job_id: str) -> None:
    while True:
        await asyncio.sleep(8)
        state = progress_state.get(job_id)
        row = storage.get(job_id)
        if not state or not row or row["status"] not in ("downloading", "processing", "uploading"):
            return
        elapsed = int(time.monotonic() - state["started"])
        stage = state["stage"]
        if stage == "downloading":
            amount = state.get("downloaded", 0)
            total = state.get("total")
            language = job_language(row)
            detail = f"{amount / 1024**2:.1f} MB" if amount else tr(language, "download_connecting")
            if total:
                detail += f" / {total / 1024**2:.1f} MB"
            text = (f"{row['title']}\n\n{selected_format(row, language)}\n"
                    f"{tr(language, 'download_stage')}\n{progress_bar(amount, total)}\n"
                    f"{detail}\n{tr(language, 'elapsed', time=f'{elapsed // 60}:{elapsed % 60:02d}')}")
        elif stage == "processing":
            text = (f"{row['title']}\n\n{tr(job_language(row), 'processing_audio')}\n"
                    f"{tr(job_language(row), 'elapsed', time=f'{elapsed // 60}:{elapsed % 60:02d}')}")
        else:
            amount = state.get("uploaded", 0)
            total = state.get("total")
            language = job_language(row)
            detail = (f"{amount / 1024**2:.1f} / {total / 1024**2:.1f} MB" if total
                      else tr(language, "upload_preparing"))
            text = (f"{row['title']}\n\n{tr(language, 'upload_to_telegram')}\n{progress_bar(amount, total)}\n"
                    f"{detail}\n{tr(language, 'elapsed', time=f'{elapsed // 60}:{elapsed % 60:02d}')}")
        link_url = PUBLIC_BASE_URL + public_path(row) if PUBLIC_BASE_URL and row["token"] else ""
        await change_status(job_id, text, link_url=link_url)


async def worker() -> None:
    global current_job
    while True:
        if disk_critical():
            await asyncio.sleep(10)
            continue
        row = storage.next_queued()
        if not row:
            queue_event.clear()
            await queue_event.wait()
            continue
        current_job = row["id"]
        heartbeat = asyncio.create_task(status_heartbeat(row["id"]))
        try:
            await process_job(row["id"])
        except Exception as exc:
            cancelled = row["id"] in cancelled_jobs
            if not cancelled:
                log.exception("Download job %s failed (selection %s)", row["id"], row["selection"])
            storage.update(row["id"], status="cancelled" if cancelled else "failed", error=str(exc)[:400])
            text = unavailable_text(row["owner_id"]) if disk_critical() else (
                "Загрузка отменена." if cancelled else f"Ошибка: {str(exc)[:350]}")
            await change_status(row["id"], text, force=True)
            if not storage.get(row["id"])["file_path"]:
                shutil.rmtree(DATA_DIR / "jobs" / row["id"], ignore_errors=True)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            progress_state.pop(row["id"], None)
            last_progress.pop(row["id"], None)
            cancelled_jobs.discard(row["id"])
            current_job = None


async def cleaner() -> None:
    next_prune = 0
    while True:
        now = int(time.time())
        if now >= next_prune:
            storage.prune(now)
            next_prune = now + 3600
        for row in storage.expired_files(now):
            if row["status"] == "uploading" or row["id"] == current_job:
                continue
            path = Path(row["file_path"])
            try:
                path.unlink(missing_ok=True)
                shutil.rmtree(path.parent, ignore_errors=True)
                storage.update(row["id"], file_path=None, token=None, delete_after=None)
            except OSError:
                log.exception("Cleanup failed for %s", row["id"])
        await asyncio.sleep(60)


async def disk_guard() -> None:
    global low_disk_mode
    while True:
        critical = disk_critical()
        if critical and not low_disk_mode:
            low_disk_mode = True
            if current_job:
                cancelled_jobs.add(current_job)
            if OWNER_ID:
                try:
                    await bot.send_message(OWNER_ID, unavailable_text(OWNER_ID))
                except Exception as exc:
                    log.warning("Could not notify owner about low disk: %s", type(exc).__name__)
        elif not critical and low_disk_mode:
            low_disk_mode = False
            if OWNER_ID:
                try:
                    await bot.send_message(OWNER_ID, "Свободное место восстановлено. Бот снова принимает ссылки.")
                except Exception as exc:
                    log.warning("Could not notify owner about recovered disk: %s", type(exc).__name__)
            queue_event.set()
        await asyncio.sleep(10)


async def refresh_pending_cards() -> None:
    await asyncio.sleep(60)
    if current_job or storage.next_queued():
        return
    rows = storage.db.execute(
        "SELECT * FROM jobs WHERE status='awaiting_selection' AND status_message_id IS NOT NULL ORDER BY created_at DESC LIMIT 3"
    ).fetchall()
    for row in rows:
        try:
            info = await fetch_metadata(row["url"])
            if row["status_is_photo"]:
                await bot.edit_message_caption(
                    chat_id=row["owner_id"], message_id=row["status_message_id"],
                    caption=card_text(info), parse_mode="HTML", reply_markup=card_buttons(
                        row["id"], info, storage.settings(row["requester_id"] or row["owner_id"])["language"]),
                )
            else:
                await bot.edit_message_text(
                    chat_id=row["owner_id"], message_id=row["status_message_id"],
                    text=card_text(info), parse_mode="HTML", reply_markup=card_buttons(
                        row["id"], info, storage.settings(row["requester_id"] or row["owner_id"])["language"]),
                )
            storage.update(row["id"], metadata=json.dumps(info, ensure_ascii=False))
        except Exception as exc:
            log.warning("Could not refresh card %s: %s", row["id"], type(exc).__name__)


async def replace_old_links() -> None:
    rows = storage.db.execute("SELECT * FROM jobs WHERE file_path IS NOT NULL AND link_expires_at>?", (int(time.time()),)).fetchall()
    for row in rows:
        if not Path(row["file_path"]).is_file() or (row["token"] and len(row["token"]) == 32 and all(c in "0123456789abcdef" for c in row["token"])):
            continue
        new_status = "link_only" if row["status"] == "failed" else row["status"]
        storage.update(row["id"], token=secrets.token_hex(16), status=new_status)
        updated = storage.get(row["id"])
        if updated["status_message_id"] and PUBLIC_BASE_URL:
            await change_status(
                row["id"],
                f"{row['title']}\nОтправка файла в Telegram ещё настраивается.",
                force=True, link_url=PUBLIC_BASE_URL + public_path(updated),
            )


async def error_log_cleaner() -> None:
    while True:
        prune_error_logs(DATA_DIR)
        await asyncio.sleep(3600)


async def main() -> None:
    global bot, storage, BOT_USERNAME
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    configure_error_log(DATA_DIR)
    prune_error_logs(DATA_DIR)
    storage = Storage(DATA_DIR / "jobs.sqlite3")
    APPROVED_USER_IDS.update(storage.approved_users())
    storage.recover()
    removed = storage.remove_orphaned_job_dirs(DATA_DIR / "jobs")
    if removed:
        log.info("Removed %s orphaned job directories on startup", removed)
    if LOCAL_API_URL:
        session = AiohttpSession(api=TelegramAPIServer.from_base(LOCAL_API_URL, is_local=True))
        bot = Bot(BOT_TOKEN, session=session)
    else:
        session = AiohttpSession(api=TelegramAPIServer.from_base(os.environ["CLOUD_API_URL"], is_local=False))
        bot = Bot(BOT_TOKEN, session=session)
    BOT_USERNAME = (await bot.get_me()).username or ""
    runner = await start_server(storage, os.environ["LINK_BIND_HOST"],
                                int(os.environ["LINK_BIND_PORT"]),
                                os.environ.get("TLS_CERT_FILE", ""), os.environ.get("TLS_KEY_FILE", ""),
                                int(os.environ.get("LEGACY_LINK_PORT", "0")))
    async def configure_commands() -> None:
        if not OWNER_ID:
            return
        try:
            await bot.set_my_commands(
                [BotCommand(command="start", description="Как пользоваться"),
                 BotCommand(command="dd", description="Скачать видео по ссылке"),
                 BotCommand(command="status", description="Состояние сервера и очереди"),
                 BotCommand(command="admin", description="Панель администратора"),
                 BotCommand(command="help", description="Справка"),
                 BotCommand(command="cancel", description="Отменить текущую задачу")],
                scope=BotCommandScopeChat(chat_id=OWNER_ID),
            )
            await bot.set_my_commands(
                [BotCommand(command="dd", description="Скачать видео по ссылке"),
                 BotCommand(command="help", description="Справка")],
                scope=BotCommandScopeAllGroupChats(),
            )
        except Exception as exc:
            log.warning("Could not set bot commands: %s", type(exc).__name__)
    dp = Dispatcher()
    dp.include_router(router)
    required_tasks = [asyncio.create_task(worker(), name="download worker"),
                      asyncio.create_task(cleaner(), name="file cleaner"),
                      asyncio.create_task(disk_guard(), name="disk guard")]
    optional_tasks = [asyncio.create_task(replace_old_links(), name="link refresh"),
                      asyncio.create_task(configure_commands(), name="bot commands"),
                      asyncio.create_task(error_log_cleaner(), name="error log cleaner")]
    polling_task = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
        name="Telegram polling",
    )
    try:
        done, _ = await asyncio.wait([polling_task, *required_tasks], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                raise RuntimeError(f"{task.get_name()} stopped unexpectedly")
            error = task.exception()
            if error:
                raise RuntimeError(f"{task.get_name()} failed") from error
            if task is not polling_task:
                raise RuntimeError(f"{task.get_name()} stopped unexpectedly")
    finally:
        for task in [polling_task, *required_tasks, *optional_tasks]:
            task.cancel()
        await asyncio.gather(polling_task, *required_tasks, *optional_tasks, return_exceptions=True)
        await runner.cleanup()
        await bot.session.close()


def run() -> None:
    with (DATA_DIR / "bot.lock").open("a+") as lock_file:
        os.chmod(lock_file.name, 0o600)
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another bot process already owns the job queue")
        asyncio.run(main())
