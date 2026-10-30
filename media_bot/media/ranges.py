"""Parsing and bounds for user-selected media clip ranges."""

from __future__ import annotations

import re


MAX_CLIP_SECONDS = 10 * 60
_RANGE = re.compile(r"^\s*(\d{1,3}(?::\d{1,2}){1,2})\s*[-–—]\s*(\d{1,3}(?::\d{1,2}){1,2})\s*$")


def _seconds(value: str) -> int:
    parts = [int(part) for part in value.split(":")]
    if any(part < 0 for part in parts):
        raise ValueError("Invalid time")
    if len(parts) == 2:
        minutes, seconds = parts
        if seconds >= 60:
            raise ValueError("Invalid time")
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        if minutes >= 60 or seconds >= 60:
            raise ValueError("Invalid time")
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError("Invalid time")


def parse_clip_range(value: str) -> tuple[int, int]:
    match = _RANGE.fullmatch(value)
    if not match:
        raise ValueError("Use MM:SS–MM:SS or HH:MM:SS–HH:MM:SS")
    start, end = (_seconds(part) for part in match.groups())
    if end <= start:
        raise ValueError("End must be after start")
    if end - start > MAX_CLIP_SECONDS:
        raise ValueError("The selected fragment is too long")
    return start, end
