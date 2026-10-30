"""User menu translations loaded from JSON resources at startup."""

from __future__ import annotations

import json
from pathlib import Path


_DIRECTORY = Path(__file__).with_name("locales")
_STRINGS = {
    language: json.loads((_DIRECTORY / f"{language}.json").read_text(encoding="utf-8"))
    for language in ("ru", "en")
}


def tr(language: str, key: str, **values: object) -> str:
    template = _STRINGS.get(language, _STRINGS["ru"])[key]
    return template.format(**values) if values else template
