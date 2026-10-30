"""Isolated metadata extraction with a hard process timeout in the bot."""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlparse

from ..media.downloader import MediaError, extract_metadata
from .network_guard import install_public_network_guard
from .protocol import protocol_stream


def _safe_diagnostic(value: str) -> str:
    """Return a bounded diagnostic without signed URLs, query strings or credentials."""
    value = re.sub(r"https?://[^\s\])}]+", "[url]", value)
    value = re.sub(r"(?i)\b(token|auth|key|signature|sign)=([^\s&]+)", r"\1=[redacted]", value)
    return value[:500]


def _diagnostic(exc: Exception) -> str:
    return _safe_diagnostic(f"{type(exc).__name__}: {exc}")


def main() -> None:
    output = protocol_stream()
    install_public_network_guard()
    try:
        host = (urlparse(sys.argv[1]).hostname or "").lower()
        if (host in {"music.yandex.ru", "music.yandex.com", "www.music.yandex.ru", "www.music.yandex.com"}
                and not os.environ.get("YANDEX_MUSIC_TOKEN", "").strip()):
            raise MediaError("Yandex Music is not configured")
        result = {"ok": True, "info": extract_metadata(sys.argv[1])}
    except MediaError as exc:
        host = (urlparse(sys.argv[1]).hostname or "").lower()
        if host in {"music.yandex.ru", "music.yandex.com", "www.music.yandex.ru", "www.music.yandex.com"}:
            if not os.environ.get("YANDEX_MUSIC_TOKEN", "").strip():
                result = {"ok": False, "error_code": "yandex_not_configured",
                          "diagnostic": "YANDEX_MUSIC_TOKEN is not configured"}
            else:
                result = {"ok": False, "error_code": "yandex_metadata_error",
                          "diagnostic": _safe_diagnostic(exc.diagnostic or _diagnostic(exc))}
        elif host in {"open.spotify.com", "spotify.link"} or host.endswith(".spotify.com"):
            result = {"ok": False, "error_code": "spotify_unavailable",
                      "diagnostic": _safe_diagnostic(exc.diagnostic or _diagnostic(exc))}
        else:
            result = {"ok": False, "error": str(exc),
                      "diagnostic": _safe_diagnostic(exc.diagnostic or _diagnostic(exc))}
    except Exception as exc:
        host = (urlparse(sys.argv[1]).hostname or "").lower()
        code = "yandex_metadata_error" if host in {"music.yandex.ru", "music.yandex.com"} else "metadata_error"
        result = {"ok": False, "error_code": code, "diagnostic": _diagnostic(exc)}
    print(json.dumps(result, ensure_ascii=False), file=output, flush=True)


if __name__ == "__main__":
    main()
