"""Regression checks for public links and cleanup after interrupted downloads."""

from __future__ import annotations

import sys
import asyncio
import tempfile
import time
import unittest
import subprocess
import os
import json
import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_bot.web.links import public_filename
from media_bot.media.kinescope import manifest_from_page
from media_bot.i18n import tr
from media_bot.media.downloader import estimated_size
from media_bot.media.filenames import audio_filename
from media_bot.data.storage import Storage
from media_bot.media.url_validation import validate_platform_url
from media_bot.ops.error_log import prune_error_logs
from media_bot.ops.bug_reports import BugReportStore
from media_bot.media import yandex_music
from yandex_auth import save_token
from media_bot.app import audio_output_title, available_file_limit, capacity_message, admin_buttons, description_split_at, MAX_JOB_BYTES, MIN_FREE_BYTES
import media_bot.app as app_module


class BotRegressions(unittest.TestCase):
    def test_bug_report_photo_is_saved_as_open_ticket(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BugReportStore(Path(directory) / "bugreports")
            message = SimpleNamespace(
                chat=SimpleNamespace(type="private", id=1),
                from_user=SimpleNamespace(id=1),
                caption="Повторяется прогресс #bugreport",
                photo=[SimpleNamespace(file_id="photo-id", file_size=5)],
                document=None,
                message_id=25,
                answer=AsyncMock(),
            )

            async def download(_file_id, destination):
                Path(destination).write_bytes(b"image")

            fake_bot = SimpleNamespace(download=AsyncMock(side_effect=download))
            fake_storage = SimpleNamespace(settings=lambda _user_id: {"language": "ru"})
            with patch.object(app_module, "bug_reports", store), \
                 patch.object(app_module, "storage", fake_storage, create=True), \
                 patch.object(app_module, "bot", fake_bot, create=True), \
                 patch.object(app_module, "OWNER_ID", 1):
                asyncio.run(app_module.receive_bug_report_media(message))
            report = store.list("open")[0]
            self.assertEqual(report["ticket"], "BR-000001")
            self.assertEqual(store.description(1), "Повторяется прогресс")
            self.assertEqual((store.folder(1) / "screenshot.jpg").read_bytes(), b"image")
            self.assertIn("BR-000001", message.answer.await_args.args[0])

    def test_admin_bug_report_view_has_status_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BugReportStore(Path(directory) / "bugreports")
            report = store.create_pending("Прогресс дублируется", chat_id=1, message_id=2)
            (store.folder(1) / "screenshot.jpg").write_bytes(b"image")
            store.finalize(report["number"], "screenshot.jpg")
            with patch.object(app_module, "bug_reports", store):
                heading, listing = app_module.bug_report_list_screen("ru", "active", 0)
                detail = app_module.bug_report_detail_screen("ru", 1, "active", 0)
            self.assertIn("Открыто: 1", heading)
            self.assertIn("BR-000001", listing.inline_keyboard[0][0].text)
            self.assertIsNotNone(detail)
            callbacks = [button.callback_data for row in detail[1].inline_keyboard for button in row]
            self.assertIn("adm:bug_set:1:in_progress:active:0", callbacks)
            self.assertIn("adm:bug_set:1:closed:active:0", callbacks)
            self.assertIn("adm:bug_photo:1", callbacks)

    def test_extra_description_replies_to_original_video(self):
        fake_bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(app_module, "bot", fake_bot, create=True):
            asyncio.run(app_module.send_description_continuation(123, "слово " * 850, 456))
        self.assertGreater(len(fake_bot.send_message.await_args_list), 1)
        for call in fake_bot.send_message.await_args_list:
            self.assertEqual(call.args[0], 123)
            self.assertEqual(call.kwargs["reply_parameters"].message_id, 456)

    def test_long_description_keeps_next_timing_line_together(self):
        description = "Первый абзац\n11:00 Почему это случилось\n12:30 Следующая тема"
        cut = description_split_at(description, 20)
        self.assertEqual(description[:cut].rstrip(), "Первый абзац")
        self.assertTrue(description[cut:].startswith("11:00 Почему"))

    def test_yandex_admin_button_uses_short_status_in_both_languages(self):
        for language, connected, disconnected in (
            ("ru", "🎵 Яндекс Музыка · подключена", "🎵 Яндекс Музыка · не подключена"),
            ("en", "🎵 Yandex Music · connected", "🎵 Yandex Music · not connected"),
        ):
            with patch.dict(os.environ, {"YANDEX_MUSIC_TOKEN": "test-token"}):
                self.assertEqual(admin_buttons(language).inline_keyboard[2][0].text, connected)
            with patch.dict(os.environ, {"YANDEX_MUSIC_TOKEN": ""}):
                self.assertEqual(admin_buttons(language).inline_keyboard[2][0].text, disconnected)

    def test_download_capacity_distinguishes_file_limit_from_disk_reserve(self):
        configured = available_file_limit(MAX_JOB_BYTES + MIN_FREE_BYTES + 1)
        self.assertEqual(configured, MAX_JOB_BYTES)
        self.assertIn("по настройке сервера", capacity_message(configured + 1, configured))
        disk_limited = available_file_limit(MIN_FREE_BYTES + 500 * 1024**2)
        self.assertEqual(disk_limited, 500 * 1024**2)
        self.assertIn("с учётом резерва диска", capacity_message(disk_limited + 1, disk_limited))

    def test_audio_filenames_transliterate_title_and_do_not_add_codec_digits(self):
        self.assertEqual(audio_filename("Туган тел", ".mp3"), "Tugan_tel.mp3")
        self.assertEqual(audio_filename("Туган тел", ".m4a"), "Tugan_tel.m4a")

    def test_audio_output_title_uses_artist_track_format_once(self):
        row = {"title": "Лучшая девушка в СССР", "selection": "audio:m4a"}
        info = {"channel": "Messer Fur Frau Muller"}
        self.assertEqual(audio_output_title(row, info),
                         "Messer Fur Frau Muller - Лучшая девушка в СССР")
        row["title"] = "Messer Fur Frau Muller - Лучшая девушка в СССР"
        self.assertEqual(audio_output_title(row, info),
                         "Messer Fur Frau Muller - Лучшая девушка в СССР")

    def test_yandex_download_stream_writes_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "track.mp3"
            response = MagicMock()
            response.status_code = 200
            response.headers = {"Content-Length": "6"}
            response.iter_content.return_value = iter([b"abc", b"def"])
            session = MagicMock()
            session.get.return_value.__enter__.return_value = response
            session.__enter__.return_value = session
            events = []
            with patch.object(yandex_music.requests, "Session", return_value=session), \
                 patch.object(yandex_music.socket, "getaddrinfo", return_value=[
                     (2, 1, 6, "", ("93.184.216.34", 443))]):
                yandex_music._download_stream("https://music.example/track.mp3", target,
                                              10, 0, lambda size, total: events.append((size, total)))
            self.assertEqual(target.read_bytes(), b"abcdef")
            self.assertEqual(events, [(3, 6), (6, 6)])
            self.assertTrue(session.get.call_args.kwargs["stream"])

    def test_device_token_is_saved_in_private_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.env"
            path.write_text("BOT_TOKEN=fake\nYANDEX_MUSIC_TOKEN=old\n")
            path.chmod(0o600)
            save_token(path, "new-token")
            self.assertEqual(path.read_text(), "BOT_TOKEN=fake\nYANDEX_MUSIC_TOKEN=new-token\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_audio_edit_worker_converts_an_existing_file(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("FFmpeg is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            result = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                                     "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(source)],
                                    capture_output=True, check=True)
            output_dir = root / "output"
            result = subprocess.run([sys.executable, "-m", "media_bot.workers.audio_edit",
                                     str(source), str(output_dir), "mp3", "128", "0", "1"],
                                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=True)
            event = json.loads(result.stdout.strip())
            self.assertEqual(event["type"], "result")
            self.assertTrue(Path(event["path"]).is_file())
            probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                    "-of", "default=noprint_wrappers=1:nokey=1", event["path"]],
                                   capture_output=True, text=True, check=True)
            self.assertLess(float(probe.stdout), 1.2)

    def test_yandex_track_links_and_full_track_metadata(self):
        self.assertEqual(yandex_music.track_reference(
            "https://music.yandex.ru/album/19091827/track/94284540?utm_source=share"),
            ("94284540", "19091827"))
        with self.assertRaises(ValueError):
            yandex_music.track_reference("https://music.yandex.ru/album/19091827")
        track = SimpleNamespace(
            id=94284540, title="Песня", duration_ms=120000, cover_uri="avatars.yandex.net/get/%%",
            artists=[SimpleNamespace(name="Исполнитель")],
            get_cover_url=lambda size: f"https://covers.example/{size}",
            get_download_info=lambda: [SimpleNamespace(codec="mp3", preview=True, bitrate_in_kbps=128),
                                       SimpleNamespace(codec="mp3", preview=False, bitrate_in_kbps=320)],
        )
        with patch.object(yandex_music, "_track", return_value=track):
            info = yandex_music.metadata("https://music.yandex.ru/track/94284540")
        self.assertEqual(info["title"], "Песня")
        self.assertEqual(info["formats"][0]["abr"], 320)
        self.assertEqual(info["duration"], 120)

    def test_yandex_preview_only_is_rejected(self):
        track = SimpleNamespace(get_download_info=lambda: [
            SimpleNamespace(codec="mp3", preview=True, bitrate_in_kbps=128)])
        with patch.object(yandex_music, "_track", return_value=track):
            with self.assertRaisesRegex(ValueError, "ознакомительный"):
                yandex_music.metadata("https://music.yandex.ru/track/94284540")

    def test_worker_protocol_ignores_unstructured_stdout(self):
        script = ('from media_bot.workers.protocol import protocol_stream; '
                  'import os; output = protocol_stream(); '
                  'os.write(1, b"[download] noisy progress\\n"); '
                  'print("{\\"type\\":\\"result\\"}", file=output, flush=True)')
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), '{"type":"result"}')

    def test_error_log_removes_files_older_than_two_days(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            logs = root / "logs"
            logs.mkdir()
            old = logs / "workers-2020-01-01.log"
            new = logs / "workers-2026-10-25.log"
            active = logs / "errors.log"
            for path in (old, new, active):
                path.write_text("error")
            now = time.time()
            os.utime(old, (now - 3 * 86400, now - 3 * 86400))
            self.assertEqual(prune_error_logs(root, now), 1)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(active.exists())

    def test_locales_include_menu_and_settings(self):
        for language in ("ru", "en"):
            self.assertIn("MFD.ddmedia", tr(language, "welcome"))
            self.assertTrue(tr(language, "menu_files"))
            self.assertTrue(tr(language, "clear_confirm"))

    def test_kinescope_signed_manifest_is_decoded(self):
        uuid = "6064076b-42e9-4e0d-b4ff-a812315bb4bb"
        page = (f'<meta content="https://kinescope.io/{uuid}/master.m3u8?'
                'expires=1793000055\\u0026sign=abc123\\u0026token=">')
        self.assertEqual(manifest_from_page(page),
                         f"https://kinescope.io/{uuid}/master.m3u8?expires=1793000055&sign=abc123&token=")

    def test_kinescope_is_accepted_but_other_hosts_are_not(self):
        self.assertIsNone(validate_platform_url(
            "https://kinescope.io/embed/cUmPgygUktYyShib8WqSzZ?t=0"))
        self.assertIsNotNone(validate_platform_url("https://127.0.0.1/watch?v=1"))

    def test_vk_live_link_gets_specific_error(self):
        error = validate_platform_url("https://vkvideo.ru/live-60345511_456239064")
        self.assertIn("Прямая трансляция", error)

    def test_hls_video_size_is_estimated_from_variant_bitrate(self):
        info = {"duration": 2860, "formats": [
            {"height": 480, "width": 854, "ext": "mp4", "vcodec": "avc1.4D401F",
             "acodec": "none", "size": None, "tbr": 54.88},
        ]}
        self.assertGreater(estimated_size(info, "video:480"), 18 * 1024**2)

    def test_video_estimate_prefers_measured_size_at_same_resolution(self):
        info = {"duration": 3061, "formats": [
            {"height": 1080, "width": 1920, "ext": "mp4", "vcodec": "avc1.640028",
             "acodec": "none", "size": None, "tbr": 4714},
            {"height": 1080, "width": 1920, "ext": "mp4", "vcodec": "avc1.640028",
             "acodec": "none", "size": 1_092_746_894, "tbr": 2856},
            {"height": None, "width": None, "ext": "m4a", "vcodec": "none",
             "acodec": "mp4a.40.2", "size": 49_533_543, "abr": 129},
        ]}
        self.assertEqual(estimated_size(info, "video:1080"), 1_142_280_437)

    def test_public_filename_supports_best_and_portrait(self):
        row = {"title": "Test", "selection": "video:720:portrait",
               "token": "a" * 32, "file_path": "/tmp/media.mp4"}
        self.assertEqual(public_filename(row), f"Test_{'a' * 32}_720_portraitp.mp4")
        row["selection"] = "video:best"
        self.assertEqual(public_filename(row), f"Test_{'a' * 32}_bestp.mp4")

    def test_audio_public_filename_includes_transliterated_artist_and_track(self):
        row = {"title": "Лучшая девушка в СССР", "selection": "audio:m4a",
               "metadata": json.dumps({"channel": "Messer Fur Frau Muller"}),
               "token": "a" * 32, "file_path": "/tmp/audio.m4a"}
        self.assertEqual(public_filename(row),
                         f"Messer_Fur_Frau_Muller-Luchshaya_devushka_v_SSSR_{'a' * 32}_m4a.m4a")

        # Metadata sometimes already contains the artist in the title; do not repeat it.
        row["title"] = "Messer Fur Frau Muller - Лучшая девушка в СССР"
        self.assertEqual(public_filename(row),
                         f"Messer_Fur_Frau_Muller-Luchshaya_devushka_v_SSSR_{'a' * 32}_m4a.m4a")

    def test_orphan_cleanup_preserves_ready_file_and_prunes_old_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Storage(root / "jobs.sqlite3")
            files = root / "jobs"
            files.mkdir()
            keep_id = store.add_job(1, "https://youtu.be/abcdefghijk", "Test", {}, "video:720", 1)
            keep_dir = files / keep_id
            keep_dir.mkdir()
            keep_file = keep_dir / "media.mp4"
            keep_file.write_bytes(b"ready")
            store.update(keep_id, file_path=str(keep_file), status="sent")
            orphan = files / "interrupted"
            orphan.mkdir()
            (orphan / "partial.mp4").write_bytes(b"partial")
            self.assertEqual(store.remove_orphaned_job_dirs(files), 1)
            self.assertTrue(keep_file.exists())
            self.assertFalse(orphan.exists())

            store.log_message(1, None, "text", "old")
            store.db.execute("UPDATE message_log SET created_at=?", (int(time.time()) - 31 * 86400,))
            store.db.commit()
            store.prune(int(time.time()))
            self.assertEqual(store.db.execute("SELECT count(*) FROM message_log").fetchone()[0], 0)
            store.db.close()

    def test_history_is_grouped_by_user_and_files_are_private(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Storage(Path(directory) / "jobs.sqlite3")
            store.log_message(101, "alice", "text", "first")
            store.log_message(202, "bob", "text", "private")
            store.log_message(101, "alice", "text", "latest")
            users = store.history_users()
            self.assertEqual([(row["user_id"], row["messages"]) for row in users],
                             [(101, 2), (202, 1)])
            self.assertEqual([row["body"] for row in store.history_for_user(101)],
                             ["latest", "first"])
            self.assertEqual(len(store.history_for_user(202)), 1)
            first = store.add_job(101, "https://youtu.be/abcdefghijk", "Alice file", {},
                                  "video:720", 1, requester_id=101)
            second = store.add_job(-999, "https://youtu.be/abcdefghijk", "Bob file", {},
                                   "video:720", 1, requester_id=202)
            store.update(first, status="sent")
            store.update(second, status="sent")
            self.assertEqual([row["title"] for row in store.user_files(101)], ["Alice file"])
            self.assertEqual([row["title"] for row in store.user_files(202)], ["Bob file"])
            store.clear_user_files(101)
            self.assertEqual(store.user_file_count(101), 0)
            self.assertEqual(store.user_file_count(202), 1)
            store.set_language(101, "en")
            self.assertEqual(store.settings(101)["language"], "en")
            self.assertFalse(store.toggle_setting(101, "apple_compatible"))
            store.db.close()


if __name__ == "__main__":
    unittest.main()
