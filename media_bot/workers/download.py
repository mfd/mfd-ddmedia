"""Run yt-dlp and FFmpeg in a process group controlled by the bot."""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from ..media.downloader import MediaError, download_media
from .network_guard import install_public_network_guard
from .protocol import protocol_stream


_output = None


def emit(kind: str, **fields: object) -> None:
    print(json.dumps({"type": kind, **fields}, ensure_ascii=False), file=_output, flush=True)


def main() -> None:
    global _output
    _output = protocol_stream()
    install_public_network_guard()
    url, selection, directory, max_file, min_free, max_temp, apple_compatible, *clip = sys.argv[1:]
    clip_range = (int(clip[0]), int(clip[1])) if len(clip) == 2 else None
    last_progress = 0.0

    def progress(amount: int, total: int | None) -> None:
        nonlocal last_progress
        now = time.monotonic()
        if now - last_progress >= 1 or (total and amount >= total):
            last_progress = now
            emit("progress", downloaded=amount, total=total)

    try:
        result = download_media(url, selection, Path(directory), int(max_file),
                                int(min_free), int(max_temp), progress, lambda: False,
                                apple_compatible == "1", clip_range)
    except MediaError as exc:
        print(f"MediaError: {exc}", file=sys.stderr, flush=True)
        emit("error", message=str(exc)[:250])
    except Exception:
        traceback.print_exc(file=sys.stderr)
        emit("error", message="Не удалось скачать или подготовить файл.")
    else:
        emit("result", path=str(result))


if __name__ == "__main__":
    main()
