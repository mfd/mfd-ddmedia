"""Keep machine-readable worker messages separate from extractor output."""

from __future__ import annotations

import os
import sys
from typing import TextIO


def protocol_stream() -> TextIO:
    # yt-dlp's HLS downloader writes progress directly to file descriptor 1,
    # even when its Python API is configured as quiet. Preserve a private copy
    # of the pipe for JSON and send all other stdout writes to /dev/null.
    stream = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    null_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(null_fd, sys.stdout.fileno())
    finally:
        os.close(null_fd)
    return stream
