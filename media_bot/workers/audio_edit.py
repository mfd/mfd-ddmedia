"""Convert or trim an existing audio file inside the queued worker process group."""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path

from .protocol import protocol_stream


def main() -> None:
    output = protocol_stream()

    def emit(kind: str, **fields: object) -> None:
        print(json.dumps({"type": kind, **fields}, ensure_ascii=False), file=output, flush=True)

    source_arg, directory_arg, codec, bitrate_arg, start_arg, end_arg = sys.argv[1:]
    source = Path(source_arg)
    directory = Path(directory_arg)
    try:
        bitrate = int(bitrate_arg)
        start = int(start_arg)
        end = int(end_arg)
        if codec not in {"mp3", "m4a"} or bitrate not in {64, 128, 192, 256, 320}:
            raise ValueError("Недопустимый формат или битрейт.")
        if start < 0 or end < 0 or (end and end <= start) or (end - start > 600 if end else False):
            raise ValueError("Неверные границы фрагмента.")
        if not source.is_file() or source.stat().st_size == 0:
            raise ValueError("Исходный аудиофайл больше не хранится на сервере.")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / ("edited." + codec)
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if start:
            command += ["-ss", str(start)]
        command += ["-i", str(source)]
        if end:
            command += ["-t", str(end - start)]
        command += ["-vn", "-threads", "1", "-c:a", "libmp3lame" if codec == "mp3" else "aac",
                    "-b:a", f"{bitrate}k", str(target)]
        result = subprocess.run(command, capture_output=True, timeout=14400, check=False)
        if result.returncode or not target.is_file() or target.stat().st_size == 0:
            raise ValueError("FFmpeg не смог обработать этот аудиофайл.")
        emit("result", path=str(target))
    except ValueError as exc:
        emit("error", message=str(exc))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        emit("error", message="Не удалось обработать аудиофайл.")


if __name__ == "__main__":
    main()
