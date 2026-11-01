"""Fast URL shape checks for the media links this bot accepts."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
INSTAGRAM_CODE = re.compile(r"^[A-Za-z0-9_-]+$")
TIKTOK_VIDEO_ID = re.compile(r"^\d+$")
SUPPORTED_HOSTS = {
    "youtube.com", "youtube-nocookie.com", "youtube-kids.com", "youtu.be",
    "instagram.com", "tiktok.com", "tiktokv.com", "vm.tiktok.com", "vt.tiktok.com",
    "vk.com", "vkvideo.ru", "x.com", "twitter.com", "facebook.com", "fb.watch",
    "vimeo.com", "player.vimeo.com",
    "music.yandex.ru", "music.yandex.com", "kinescope.io",
    "soundcloud.com", "on.soundcloud.com", "mixcloud.com",
    "open.spotify.com", "spotify.link",
}


def supported_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host in SUPPORTED_HOSTS or (host.endswith(".youtube.com") and host != ".youtube.com")


def validate_platform_url(url: str) -> str | None:
    """Return a user-facing error for malformed known-platform URLs, else None."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".").removeprefix("www.").removeprefix("m.")
    path = parsed.path.rstrip("/")
    parts = [part for part in path.split("/") if part]

    if (host == "youtube.com" or host.endswith(".youtube.com")
            or host in {"youtube-nocookie.com", "youtube-kids.com"}):
        video_id = parse_qs(parsed.query).get("v", [""])[0]
        if path in {"/watch", "/watch.php", "/watch_popup", "/movie"} and YOUTUBE_VIDEO_ID.fullmatch(video_id):
            return None
        if len(parts) == 2 and parts[0] in {"shorts", "live", "embed", "v", "e"} and YOUTUBE_VIDEO_ID.fullmatch(parts[1]):
            return None
        return "Пришлите ссылку на отдельный ролик YouTube: watch?v=…, shorts/… или youtu.be/… . Ссылки на каналы и списки не подходят."

    if host == "youtu.be":
        if len(parts) == 1 and YOUTUBE_VIDEO_ID.fullmatch(parts[0]):
            return None
        return "Не удалось распознать ссылку YouTube. Пришлите короткую ссылку именно на ролик."

    if host == "instagram.com":
        is_media_post = (
            len(parts) == 2
            and parts[0] in {"p", "reel", "reels", "tv"}
            and not (parts[0] == "reels" and parts[1] == "audio")
            and INSTAGRAM_CODE.fullmatch(parts[1])
        )
        if is_media_post:
            return None
        if len(parts) == 2 and parts[0] == "stories" and re.fullmatch(r"[A-Za-z0-9._]+", parts[1]):
            return None
        if (len(parts) == 3 and parts[0] == "stories"
                and re.fullmatch(r"[A-Za-z0-9._]+", parts[1]) and parts[2].isdigit()):
            return None
        return "Пришлите прямую ссылку Instagram на публикацию, Reel или Story. Ссылки на профиль и главную страницу не подходят."

    if host in {"tiktok.com", "tiktokv.com"}:
        if len(parts) == 3 and parts[0].startswith("@") and parts[1] == "video" and TIKTOK_VIDEO_ID.fullmatch(parts[2]):
            return None
        if len(parts) == 3 and parts[0] == "share" and parts[1] == "video" and TIKTOK_VIDEO_ID.fullmatch(parts[2]):
            return None
        if len(parts) == 2 and parts[0] == "embed" and TIKTOK_VIDEO_ID.fullmatch(parts[1]):
            return None
        return "Пришлите прямую ссылку TikTok на отдельный ролик, а не на профиль или подборку."

    # Short TikTok links redirect to a supported video URL; let yt-dlp resolve them.
    if host in {"vm.tiktok.com", "vt.tiktok.com"}:
        return None if len(parts) == 1 and re.fullmatch(r"[A-Za-z0-9]+", parts[0]) else "Не удалось распознать короткую ссылку TikTok."
    if host in {"vimeo.com", "player.vimeo.com"}:
        is_video = (
            host == "player.vimeo.com" and len(parts) == 2
            and parts[0] == "video" and parts[1].isdigit()
        ) or (
            host == "vimeo.com" and (
                (len(parts) == 1 and parts[0].isdigit())
                or (len(parts) == 2 and parts[0] == "video" and parts[1].isdigit())
                or (len(parts) == 2 and parts[0].isdigit() and re.fullmatch(r"[A-Za-z0-9]+", parts[1]))
            )
        )
        return None if is_video else "Пришлите прямую ссылку Vimeo на отдельное видео."
    if host == "kinescope.io":
        if ((len(parts) == 2 and parts[0] == "embed") or len(parts) == 1) and re.fullmatch(r"[A-Za-z0-9_-]{8,64}", parts[-1]):
            return None
        return "Пришлите прямую ссылку Kinescope на одно видео."
    if host == "vkvideo.ru" and re.fullmatch(r"/live-\d+_\d+", path):
        return ("Прямая трансляция VK сейчас не поддерживается. "
                "После завершения эфира пришлите ссылку на его запись.")
    if not supported_host(host):
        return ("Этот сайт пока не поддерживается. Пришлите ссылку на YouTube, Vimeo, Instagram, TikTok, "
                "SoundCloud, Mixcloud, Kinescope, VK, X, Facebook или Яндекс Музыку.")
    return None
