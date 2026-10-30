"""Safe, readable filenames for delivered media."""

from __future__ import annotations

import re
from pathlib import Path


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_TRANSLIT.update({key.upper(): value.capitalize() for key, value in tuple(_TRANSLIT.items())})


def transliterated_stem(title: str, limit: int = 100) -> str:
    text = "".join(_TRANSLIT.get(char, char) for char in title)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._-")
    stem = re.sub(r"_+", "_", stem)[:limit].rstrip("._-")
    return stem or "media"


def audio_stem(title: str, artist: str = "", limit: int = 100) -> str:
    """Build a consistent ``Artist-Track_title`` stem for audio files and links."""
    title = str(title or "media").strip()
    artist = str(artist or "").strip()
    if artist and artist.casefold() not in title.casefold():
        title = f"{artist}-{title}"
    # Keep the compact trim range readable and identical in filenames and captions.
    # Brackets are normally removed by transliterated_stem's safe-character filter.
    match = re.search(r"\s*[–—-]\s*(\[\d{4}-\d{4}m\])\s*$", title)
    range_suffix = ""
    if match:
        title = title[:match.start()].rstrip()
        range_suffix = f"-{match.group(1)}"
    # Avoid spaces surrounding the author/title separator in the filename.
    title = re.sub(r"\s+[-–—]\s+", "-", title)
    base = transliterated_stem(title, limit=max(1, limit - len(range_suffix)))
    return f"{base}{range_suffix}"


def audio_filename(title: str, extension: str) -> str:
    """Return a transliterated title-based name without codec-specific suffixes."""
    suffix = Path("media" + extension).suffix.lower()
    if suffix not in {".m4a", ".mp3"}:
        suffix = ".mp3"
    return f"{audio_stem(title)}{suffix}"
