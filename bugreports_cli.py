"""Read bug report tickets from the server without loading bot credentials."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from media_bot.ops.bug_reports import BugReportStore


def default_root() -> Path:
    configured = os.environ.get("BUG_REPORTS_DIR")
    if configured:
        return Path(configured)
    return Path(os.environ.get("DATA_DIR", "/home/aiagent/media-bot-data")) / "bugreports"


def main() -> None:
    parser = argparse.ArgumentParser(description="Read private bug report tickets")
    parser.add_argument("--root", type=Path, default=default_root())
    subcommands = parser.add_subparsers(dest="command", required=True)
    listing = subcommands.add_parser("list", help="List active tickets by default")
    listing.add_argument("--status", choices=("active", "open", "in_progress", "closed", "all"),
                         default="active")
    listing.add_argument("--json", action="store_true")
    detail = subcommands.add_parser("show", help="Show one ticket and its screenshot path")
    detail.add_argument("ticket", help="Ticket number, for example BR-000001")
    detail.add_argument("--json", action="store_true")
    args = parser.parse_args()
    store = BugReportStore(args.root, create=False)
    if args.command == "list":
        reports = store.list()
        if args.status == "active":
            reports = [report for report in reports if report["status"] != "closed"]
        elif args.status != "all":
            reports = [report for report in reports if report["status"] == args.status]
        for report in reports:
            report["preview"] = " ".join(store.description(report["number"]).split())[:100]
        if args.json:
            print(json.dumps(reports, ensure_ascii=False, indent=2))
        else:
            for report in reports:
                print(f"{report['ticket']} · {report['status']} · {report['created_at']} · {report['preview']}")
            if not reports:
                print("No tickets")
        return
    ticket_number = args.ticket.removeprefix("BR-")
    if not ticket_number.isdigit():
        parser.error("Expected a ticket number such as BR-000001")
    ticket_id = int(ticket_number)
    report = store.get(ticket_id)
    if not report or report["status"] == "saving":
        parser.error("Ticket not found")
    report["description"] = store.description(ticket_id)
    report["screenshot_path"] = str(store.folder(ticket_id) / report["screenshot"])
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"{report['ticket']} · {report['status']} · {report['created_at']}")
        print(f"Screenshot: {report['screenshot_path']}")
        print()
        print(report["description"])


if __name__ == "__main__":
    main()
