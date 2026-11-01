"""Private, durable bug reports stored as one folder per ticket."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


STATUSES = frozenset({"open", "in_progress", "closed"})
TICKET_NAME = re.compile(r"BR-(\d{6})$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_private(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)


def _replace_json(path: Path, record: dict) -> None:
    temporary = path.with_name(f".ticket-{uuid.uuid4().hex}.tmp")
    try:
        _write_private(temporary, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class BugReportStore:
    def __init__(self, root: Path, *, create: bool = True) -> None:
        self.root = root
        if create:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            root.chmod(0o700)
        elif not root.is_dir():
            raise FileNotFoundError(root)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.root / ".lock"
        with lock_path.open("a+") as handle:
            lock_path.chmod(0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def folder(self, ticket_id: int) -> Path:
        if ticket_id < 1:
            raise ValueError("Invalid ticket ID")
        return self.root / f"BR-{ticket_id:06d}"

    def create_pending(self, description: str, *, chat_id: int, message_id: int) -> dict:
        if not description.strip():
            raise ValueError("A bug report needs a description")
        with self._locked():
            numbers = [int(match.group(1)) for entry in self.root.iterdir()
                       if entry.is_dir() and (match := TICKET_NAME.fullmatch(entry.name))]
            ticket_id = max(numbers, default=0) + 1
            folder = self.folder(ticket_id)
            folder.mkdir(mode=0o700)
        try:
            _write_private(folder / "description.txt", description.strip() + "\n")
            record = {
                "ticket": folder.name,
                "number": ticket_id,
                "status": "saving",
                "created_at": _now(),
                "updated_at": _now(),
                "telegram_chat_id": chat_id,
                "telegram_message_id": message_id,
                "screenshot": None,
            }
            _replace_json(folder / "ticket.json", record)
            return record
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise

    def get(self, ticket_id: int) -> dict | None:
        path = self.folder(ticket_id) / "ticket.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if record.get("number") == ticket_id else None

    def description(self, ticket_id: int) -> str:
        return (self.folder(ticket_id) / "description.txt").read_text(encoding="utf-8").strip()

    def finalize(self, ticket_id: int, screenshot: str) -> dict:
        if screenshot not in {"screenshot.jpg", "screenshot.png", "screenshot.webp"}:
            raise ValueError("Invalid screenshot name")
        with self._locked():
            record = self.get(ticket_id)
            if not record or record["status"] != "saving":
                raise ValueError("Ticket is not pending")
            if not (self.folder(ticket_id) / screenshot).is_file():
                raise FileNotFoundError(screenshot)
            record.update(status="open", screenshot=screenshot, updated_at=_now())
            _replace_json(self.folder(ticket_id) / "ticket.json", record)
            return record

    def discard_pending(self, ticket_id: int) -> None:
        with self._locked():
            record = self.get(ticket_id)
            if record and record["status"] == "saving":
                shutil.rmtree(self.folder(ticket_id))

    def set_status(self, ticket_id: int, status: str) -> dict:
        if status not in STATUSES:
            raise ValueError("Invalid ticket status")
        with self._locked():
            record = self.get(ticket_id)
            if not record or record["status"] not in STATUSES:
                raise ValueError("Ticket not found")
            record.update(status=status, updated_at=_now())
            _replace_json(self.folder(ticket_id) / "ticket.json", record)
            return record

    def list(self, status: str | None = None) -> list[dict]:
        if status is not None and status not in STATUSES:
            raise ValueError("Invalid ticket status")
        numbers = sorted((int(match.group(1)) for entry in self.root.iterdir()
                          if entry.is_dir() and (match := TICKET_NAME.fullmatch(entry.name))), reverse=True)
        records = [self.get(number) for number in numbers]
        return [record for record in records if record and record.get("status") in STATUSES
                and (status is None or record["status"] == status)]
