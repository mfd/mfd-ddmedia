"""Send deployment notices to the configured owner without displaying credentials."""

from __future__ import annotations

import argparse
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path


def notification_payload(chat_id: str, message: str, *, show_changes: bool = False) -> dict[str, str]:
    payload = {"chat_id": chat_id, "text": message}
    if show_changes:
        payload["reply_markup"] = json.dumps({"inline_keyboard": [[{
            "text": "Посмотреть изменения", "callback_data": "changelog:latest",
        }]]}, ensure_ascii=False)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a deployment notice to the bot owner")
    parser.add_argument("--show-changes", action="store_true")
    parser.add_argument("message")
    args = parser.parse_args()
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
        data=urllib.parse.urlencode(notification_payload(owner, args.message,
                                                         show_changes=args.show_changes)).encode(),
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
