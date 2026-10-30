"""Send deployment notices to the configured owner without displaying credentials."""

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: notify.py MESSAGE", file=sys.stderr)
        return 2
    env_path = Path(os.environ["BOT_ENV_FILE"])
    values = {}
    for line in env_path.read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    token = values["BOT_TOKEN"]
    owner = values["OWNER_ID"]
    base = values.get("LOCAL_API_URL") or values["CLOUD_API_URL"]
    request = urllib.request.Request(
        f"{base.rstrip('/')}/bot{token}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": owner, "text": sys.argv[1]}).encode(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.status != 200:
                raise RuntimeError("Notification was rejected")
    except Exception:
        print("Could not send deployment notice", file=sys.stderr)
        return 1
    print("Deployment notice delivered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
