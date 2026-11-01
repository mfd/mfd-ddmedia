"""Ticket persistence and the read-only agent interface."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))

from media_bot.ops.bug_reports import BugReportStore


class BugReportTests(unittest.TestCase):
    def test_ticket_folder_and_status_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BugReportStore(Path(directory) / "bugreports")
            report = store.create_pending("Дважды показывается прогресс", chat_id=1, message_id=2)
            self.assertEqual(report["ticket"], "BR-000001")
            self.assertEqual(store.list(), [])
            folder = store.folder(report["number"])
            self.assertEqual((folder / "description.txt").read_text().strip(),
                             "Дважды показывается прогресс")
            (folder / "screenshot.jpg").write_bytes(b"image")
            store.finalize(report["number"], "screenshot.jpg")
            self.assertEqual(store.list("open")[0]["ticket"], "BR-000001")
            store.set_status(report["number"], "in_progress")
            self.assertEqual(store.get(report["number"])["status"], "in_progress")
            store.set_status(report["number"], "closed")
            self.assertEqual(store.list("open"), [])
            self.assertEqual(store.list("closed")[0]["ticket"], "BR-000001")
            second = store.create_pending("Следующая ошибка", chat_id=1, message_id=3)
            self.assertEqual(second["ticket"], "BR-000002")

    def test_agent_cli_reads_active_and_closed_tickets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "bugreports"
            store = BugReportStore(root)
            report = store.create_pending("Таймкод не работает", chat_id=1, message_id=2)
            (store.folder(report["number"]) / "screenshot.png").write_bytes(b"image")
            store.finalize(report["number"], "screenshot.png")
            command = [sys.executable, str(PROJECT_DIR / "bugreports_cli.py"), "--root", str(root)]
            active = subprocess.run(command + ["list", "--json"], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(active.stdout)[0]["ticket"], "BR-000001")
            detail = subprocess.run(command + ["show", "BR-000001", "--json"],
                                    check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(detail.stdout)["description"], "Таймкод не работает")
            store.set_status(report["number"], "closed")
            active = subprocess.run(command + ["list", "--json"], check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(active.stdout), [])


if __name__ == "__main__":
    unittest.main()
