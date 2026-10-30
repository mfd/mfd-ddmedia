"""Private error logs with two-day retention."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


RETENTION_SECONDS = 2 * 24 * 60 * 60


def log_directory(data_dir: Path) -> Path:
    directory = data_dir / "logs"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def configure_error_log(data_dir: Path) -> None:
    path = log_directory(data_dir) / "errors.log"
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    handler = TimedRotatingFileHandler(path, when="midnight", interval=1,
                                       backupCount=2, encoding="utf-8", utc=True)
    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter("%(asctime)s UTC %(levelname)s %(name)s: %(message)s"))
    logging.getLogger("media_bot").addHandler(handler)


def worker_error_stream(data_dir: Path):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = log_directory(data_dir) / f"workers-{date}.log"
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(path, 0o600)
    return path.open("a", encoding="utf-8")


def prune_error_logs(data_dir: Path, now: float | None = None) -> int:
    directory = log_directory(data_dir)
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    removed = 0
    for path in directory.iterdir():
        if not path.is_file() or not path.name.startswith(("workers-", "errors.log.")):
            continue
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed += 1
    return removed
