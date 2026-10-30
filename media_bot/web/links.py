from __future__ import annotations

import asyncio
import datetime
import json
import mimetypes
import os
import re
import ssl
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from ..data.storage import Storage
from ..media.filenames import audio_stem, transliterated_stem


def _public_filename(row, *, transliterate: bool, include_audio_artist: bool = True) -> str:
    selection = row["selection"] or "file"
    is_audio = selection.startswith("audio:")
    metadata = json.loads(row["metadata"] or "{}") if is_audio else {}
    if transliterate:
        title = (audio_stem(row["title"], metadata.get("channel", "") if include_audio_artist else "", limit=80)
                 if is_audio else transliterated_stem(row["title"], limit=80))
    else:
        # Preserve already-issued links until their short expiry window ends.
        title = re.sub(r"[^\w]+", "_", row["title"], flags=re.UNICODE).strip("_")[:80] or "media"
    quality = selection.split(":", 1)[1] if ":" in selection else selection
    if selection.startswith("video:"):
        quality += "p"
    quality = re.sub(r"[^a-zA-Z0-9]+", "_", quality)
    extension = Path(row["file_path"] or "media.mp4").suffix.lower() or ".mp4"
    return f"{title}_{row['token']}_{quality}{extension}"


def public_filename(row) -> str:
    return _public_filename(row, transliterate=True)


def public_path(row) -> str:
    date = datetime.datetime.fromtimestamp(row["ready_at"], datetime.timezone.utc).strftime("%Y-%m-%d")
    return f"/tmp_mfdmediabot/{date}/{quote(public_filename(row))}"


def create_app(storage: Storage) -> web.Application:
    app = web.Application()
    download_slots = asyncio.Semaphore(8)

    async def home(_: web.Request) -> web.Response:
        return web.Response(
            text=("<!doctype html><html lang='ru'><meta charset='utf-8'>"
                  "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                  "<title>MFD.ddmedia</title><body style='font:18px system-ui;max-width:42rem;"
                  "margin:4rem auto;padding:0 1rem;background:#101820;color:#fff'>"
                  "<h1>MFD.ddmedia</h1><p>Отправьте ссылку боту в Telegram, выберите формат "
                  "и получите файл или временную ссылку.</p>"
                  "<p>Send a video link to the bot in Telegram, choose a format, "
                  "and receive the file or a temporary download link.</p>"
                  "<a style='color:#80caff' href='https://t.me/mfdmediabot'>Открыть бота / Open bot</a>"
                  "</body></html>"),
            content_type="text/html", charset="utf-8",
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )

    async def changelog(_: web.Request) -> web.Response:
        try:
            body = (Path(__file__).resolve().parents[2] / "CHANGELOG.txt").read_text(encoding="utf-8")
        except OSError:
            raise web.HTTPNotFound()
        return web.Response(text=body, content_type="text/plain", charset="utf-8",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    async def serve_file(request: web.Request) -> web.StreamResponse:
        filename = request.match_info["filename"]
        match = re.search(r"_([0-9a-f]{32})_[a-zA-Z0-9_]+\.[a-zA-Z0-9]+$", filename)
        if not match:
            raise web.HTTPNotFound()
        token = match.group(1)
        row = storage.by_token(token)
        if not row or not row["file_path"] or not row["link_expires_at"] or int(time.time()) >= row["link_expires_at"]:
            raise web.HTTPNotFound()
        expected_date = datetime.datetime.fromtimestamp(row["ready_at"], datetime.timezone.utc).strftime("%Y-%m-%d")
        if (request.match_info["date"] != expected_date
                or filename not in {
                    public_filename(row),
                    _public_filename(row, transliterate=True, include_audio_artist=False),
                    _public_filename(row, transliterate=False),
                }):
            raise web.HTTPNotFound()
        path = Path(row["file_path"])
        if not path.is_file():
            raise web.HTTPNotFound()
        size = path.stat().st_size
        range_header = request.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if range_header:
            if not range_header.startswith("bytes=") or "," in range_header:
                raise web.HTTPRequestRangeNotSatisfiable()
            first, sep, last = range_header[6:].partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:
                    length = int(last)
                    start = max(0, size - length)
            except ValueError:
                raise web.HTTPRequestRangeNotSatisfiable()
            if not sep or start < 0 or end >= size or start > end:
                raise web.HTTPRequestRangeNotSatisfiable()
            status = 206
        headers = {
            "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
            "Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        if request.method == "HEAD":
            return web.Response(status=status, headers=headers)
        response = web.StreamResponse(status=status, headers=headers)
        await response.prepare(request)
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = await asyncio.to_thread(handle.read, min(256 * 1024, remaining))
                    if not chunk:
                        break
                    await asyncio.wait_for(response.write(chunk), timeout=30)
                    remaining -= len(chunk)
        except (ConnectionResetError, asyncio.CancelledError, asyncio.TimeoutError):
            pass
        finally:
            try:
                await response.write_eof()
            except ConnectionResetError:
                pass
        return response

    async def file_handler(request: web.Request) -> web.StreamResponse:
        if download_slots.locked():
            raise web.HTTPServiceUnavailable(headers={"Retry-After": "5"})
        async with download_slots:
            return await serve_file(request)

    app.router.add_route("GET", "/", home)
    app.router.add_route("GET", "/CHANGELOG.txt", changelog)
    app.router.add_route("GET", "/tmp_mfdmediabot/{date}/{filename}", file_handler)
    app.router.add_route("HEAD", "/tmp_mfdmediabot/{date}/{filename}", file_handler)
    return app


async def start_server(storage: Storage, host: str, port: int, cert_file: str = "", key_file: str = "",
                       legacy_port: int = 0) -> web.AppRunner:
    runner = web.AppRunner(create_app(storage))
    await runner.setup()
    ssl_context = None
    if cert_file and key_file:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(cert_file, key_file)
    await web.TCPSite(runner, host, port, ssl_context=ssl_context).start()
    if legacy_port and legacy_port != port:
        await web.TCPSite(runner, host, legacy_port, ssl_context=ssl_context).start()
    return runner

