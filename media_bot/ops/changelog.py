"""Read the newest release entry from the plain-text changelog."""

from __future__ import annotations

import re


_HEADING = re.compile(r"^===\s+(.+?)\s+===$")


def latest_entry(contents: str) -> tuple[str, str] | None:
    version = None
    lines = []
    for line in contents.splitlines():
        match = _HEADING.fullmatch(line.strip())
        if match:
            if version is not None:
                break
            version = match.group(1)
            continue
        if version is not None:
            lines.append(line)
    if version is None:
        return None
    return version, "\n".join(lines).strip()
