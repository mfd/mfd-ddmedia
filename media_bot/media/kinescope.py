"""Read public Kinescope player pages without reusing stale signed stream URLs."""

from __future__ import annotations

import html
import re
import urllib.request
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse


_MANIFEST = re.compile(r"https://kinescope\.io/[^\s\"'<>]*?/master\.m3u8\?[^\s\"'<>]+")
_VIDEO_PATH = re.compile(r"/[0-9a-fA-F-]{36}/master\.m3u8")


class _PageMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        fields = dict(attrs)
        name = fields.get("property") or fields.get("name")
        if name and fields.get("content"):
            self.meta[name] = html.unescape(fields["content"] or "")


def manifest_from_page(page: str) -> str:
    """Extract a fresh signed URL; Kinescope escapes ampersands in page metadata."""
    for match in _MANIFEST.finditer(page):
        candidate = html.unescape(match.group(0)).replace("\\u0026", "&").replace("\\/", "/")
        parsed = urlparse(candidate)
        query = parse_qs(parsed.query)
        if (parsed.scheme == "https" and parsed.hostname == "kinescope.io"
                and _VIDEO_PATH.fullmatch(parsed.path)
                and query.get("expires") and query.get("sign")):
            return candidate
    raise ValueError("No public signed Kinescope stream found")


def public_source(page_url: str) -> tuple[str, dict[str, str]]:
    request = urllib.request.Request(page_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        if urlparse(response.url).hostname != "kinescope.io":
            raise ValueError("Unexpected Kinescope redirect")
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise ValueError("Kinescope page is too large")
    page = body.decode("utf-8", "replace")
    metadata = _PageMetadata()
    metadata.feed(page)
    return manifest_from_page(page), metadata.meta
